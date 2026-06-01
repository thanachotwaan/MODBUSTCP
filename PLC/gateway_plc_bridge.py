import urllib.request
import json
from pymodbus.client import ModbusTcpClient
from datetime import datetime
import time
import calendar
import math
import random

# ==============================================================================
# CONFIGURATION & PARAMETERS (การตั้งค่าสำหรับระบบ)
# ==============================================================================
PLC_IP = "192.168.0.5"

# API Endpoints (URLs สำหรับดึงข้อมูลจาก Edge Gateway API)
PCS_API_URL = "http://localhost:5000/api/v1/diagnostics/cache/summary"
BMS_API_URL = "http://localhost:5000/api/v1/bms/summary"
INVERTER_API_URL = "http://localhost:5000/api/v1/inverter/summary"     # Placeholder
METER_API_URL = "http://localhost:5000/api/v1/powermeter/summary"      # Placeholder
SCHEDULE_API_URL = "http://localhost:5000/api/v1/pcs/schedule"

# ==============================================================================
# STATE CACHE VARIABLES (ตัวแปรจำค่าสถานะ)
# ==============================================================================
last_active_power_val = None
last_written_m210 = None
last_written_m211 = None
last_printed_pcs_on = None
last_printed_power = None

# Calendar & HMI Sync variables (Charge)
last_hmi_month_chg = None
last_hmi_year_chg = None
active_hmi_day_chg = 0
last_hmi_statuses_chg = None
last_hmi_calendar_poll_time_chg = 0

# Calendar & HMI Sync variables (Discharge)
last_hmi_month_dis = None
last_hmi_year_dis = None
active_hmi_day_dis = 0
last_hmi_statuses_dis = None
last_hmi_calendar_poll_time_dis = 0

last_poll_active_schedule_time = 0
last_detail_vals = None

# Persistent Mock accumulators for testing (ตัวสะสมพลังงานจำลองใช้สำหรับทดสอบ)
mock_import_energy = 12450.0  # kWh (Grid Import)
mock_export_energy = 3450.0   # kWh (Grid Export)
mock_inv_total_gen = 8940.0   # kWh (Solar Inverter Generation)
mock_inv_daily_gen = 15.0     # kWh (Daily Solar Generation)
last_date_day = None          # Used to track day changes to reset daily figures

# ==============================================================================
# HELPER FUNCTIONS (ฟังก์ชันสนับสนุน)
# ==============================================================================
def to_u16(val):
    """Convert signed/unsigned integer to unsigned 16-bit integer (Two's Complement)."""
    val = int(val)
    if val < 0:
        return (val + 65536) & 0xFFFF
    return val & 0xFFFF

def to_s16(val):
    """Convert unsigned 16-bit integer to signed 16-bit integer (Two's Complement)."""
    val = int(val) & 0xFFFF
    if val >= 0x8000:
        return val - 65536
    return val

def to_u32_words(val):
    """Split a 32-bit integer into two 16-bit unsigned integers (Low Word, High Word)."""
    val = int(val) & 0xFFFFFFFF
    low_word = val & 0xFFFF
    high_word = (val >> 16) & 0xFFFF
    return [low_word, high_word]

# ==============================================================================
# REST API FUNCTIONS (ฟังก์ชันติดต่อ Gateway API)
# ==============================================================================
def fetch_month_schedule(year, month):
    """Fetch calendar schedule for the specified month and year from Gateway API."""
    url = f"{SCHEDULE_API_URL}/calendar?year={year}&month={month}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=2.0) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            if res_data.get("success"):
                return res_data.get("data", [])
    except Exception as e:
        print(f"HMI Bridge: Error fetching month schedule: {e}")
    return []

def fetch_today_schedule():
    """Fetch today's active schedule from Gateway API."""
    url = f"{SCHEDULE_API_URL}/today"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=2.0) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            if res_data.get("success"):
                return res_data.get("data")
    except Exception as e:
        print(f"HMI Bridge: Error fetching today's schedule: {e}")
    return None

def send_pcs_command(cmd_type, value):
    """Send control command (ON/OFF) to PCS Edge Gateway Web API."""
    url = "http://localhost:5000/api/v1/pcs/command"
    payload = json.dumps({
        "commandType": cmd_type,
        "value": value
    }).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=payload,
        headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=2.0) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            if res_data.get("success"):
                print(f"HMI: Sent command to PCS successfully: Type={cmd_type}, Value={value}")
                return True
            else:
                print(f"HMI: Failed to send command to PCS: {res_data.get('error')}")
    except Exception as e:
        print(f"HMI: Error sending command to PCS: {e}")
    return False

def send_active_power_command(value):
    """Send active power setpoint command to PCS Edge Gateway Web API."""
    url = "http://localhost:5000/api/v1/pcs/active-power"
    payload = json.dumps({
        "value": int(value)
    }).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=payload,
        headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=2.0) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            if res_data.get("success"):
                print(f"HMI: Sent active power demand successfully: Value={value} ({(value/10.0):.1f} kW)")
                return True
            else:
                print(f"HMI: Failed to send active power: {res_data.get('error')}")
    except Exception as e:
        print(f"HMI: Error sending active power: {e}")
    return False

# ==============================================================================
# MODULAR POLL & SYNC LOGIC (ฟังก์ชันสำหรับการดึงและแปลงข้อมูลอุปกรณ์ต่างๆ)
# ==============================================================================

def poll_pcs_telemetry(client, hmi_on_pressed, hmi_off_pressed, hmi_set_power_pressed):
    """
    ดึงข้อมูล PCS จาก API หรือสร้างข้อมูลจำลองหากออฟไลน์ และเขียนลง PLC D4300 - D4340
    (Fetch PCS telemetry from API or mock, and write to D4300 - D4340).
    Returns (is_pcs_on, pcs_power_kw)
    """
    global PCS_API_URL, last_active_power_val, last_written_m210, last_written_m211, last_printed_pcs_on, last_printed_power
    is_pcs_on = False
    pcs_power_kw = 0.0

    try:
        req = urllib.request.Request(PCS_API_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=1.0) as response:
            api_data = json.loads(response.read().decode('utf-8'))
            
            if api_data.get("success") and "data" in api_data and "allRegisters" in api_data["data"]:
                regs = api_data["data"]["allRegisters"]
                
                # block1: D4300 - D4303 (Grid Freq, V RS, V ST, V TR)
                block1 = [
                    to_u16(regs.get("0x06FF", 0)),
                    to_u16(regs.get("0x0700", 0)),
                    to_u16(regs.get("0x0701", 0)),
                    to_u16(regs.get("0x0702", 0))
                ]
                # block2: D4310 - D4312 (AC V R, AC V S, AC V T)
                block2 = [
                    to_u16(regs.get("0x0709", 0)),
                    to_u16(regs.get("0x070A", 0)),
                    to_u16(regs.get("0x070B", 0))
                ]
                # block3: D4325 - D4326 (Active Power, Reactive Power)
                block3 = [
                    to_u16(regs.get("0x0718", 0)),
                    to_u16(regs.get("0x0719", 0))
                ]
                # block4: D4340 (Ambient Temp)
                block4 = [
                    to_u16(regs.get("0x071E", 0))
                ]
                
                # Write blocks using 32768 + Offset (Modbus addresses)
                client.write_registers(37068, block1)  # D4300 maps to 37068 (32768 + 4300)
                client.write_registers(37078, block2)  # D4310 maps to 37078
                client.write_registers(37093, block3)  # D4325 maps to 37093
                client.write_registers(37108, block4)  # D4340 maps to 37108

                # Sync dashboard set power (0x1007) back to HMI input register D2110 (6206)
                if "0x1007" in regs:
                    pcs_set_power_raw = to_s16(regs["0x1007"])
                    pcs_set_power_kw = int(pcs_set_power_raw / 10)
                    
                    if last_active_power_val is None:
                        last_active_power_val = pcs_set_power_kw
                        client.write_registers(6206, [to_u16(pcs_set_power_kw)])
                        print(f"Sync: Initialized HMI D2110 to {pcs_set_power_kw} kW")
                    elif pcs_set_power_kw != last_active_power_val:
                        client.write_registers(6206, [to_u16(pcs_set_power_kw)])
                        print(f"Sync: Updated HMI D2110 to {pcs_set_power_kw} kW (from Dashboard/PCS)")
                        last_active_power_val = pcs_set_power_kw

                # ON/OFF Command execution
                state_val = regs.get("0x06F4", 1)
                is_pcs_on = state_val in [2, 3, 4, 5, 6, 10]
                pcs_power_kw = to_s16(regs.get("0x0718", 0)) / 10.0  # Scale back to kW

                if not is_pcs_on and hmi_on_pressed:
                    print("HMI Trigger: Turn ON PCS requested (M210 is True). Sending ON command...")
                    if send_pcs_command(1, 1):
                        is_pcs_on = True
                        hmi_on_pressed = False
                elif is_pcs_on and hmi_off_pressed:
                    print("HMI Trigger: Turn OFF PCS requested (M211 is True). Sending OFF command...")
                    if send_pcs_command(1, 0):
                        is_pcs_on = False
                        hmi_off_pressed = False

                m210_val = is_pcs_on
                m211_val = not is_pcs_on
                client.write_coil(2258, m210_val)  # M210
                client.write_coil(2259, m211_val)  # M211
                last_written_m210 = m210_val
                last_written_m211 = m211_val

                if is_pcs_on != last_printed_pcs_on or pcs_power_kw != last_printed_power:
                    print(f"--> PLC WRITE SUCCESS: PCS State changed to On={is_pcs_on}, Power={pcs_power_kw}kW")
                    last_printed_pcs_on = is_pcs_on
                    last_printed_power = pcs_power_kw
                
                return is_pcs_on, pcs_power_kw
    except Exception as e:
        print(f"Failed to fetch PCS telemetry (API offline or error): {e}")

    # --------------------------------------------------------------------------
    # FALLBACK MOCK DATA FOR PCS (ข้อมูลจำลองหากอุปกรณ์ออฟไลน์)
    # --------------------------------------------------------------------------
    mock_freq = 50.0 + random.uniform(-0.03, 0.03)
    mock_v_rs = 398.0 + random.uniform(-1.5, 1.5)
    mock_v_st = 397.0 + random.uniform(-1.5, 1.5)
    mock_v_tr = 397.5 + random.uniform(-1.5, 1.5)
    mock_v_r = 229.5 + random.uniform(-1.0, 1.0)
    mock_v_s = 230.2 + random.uniform(-1.0, 1.0)
    mock_v_t = 229.8 + random.uniform(-1.0, 1.0)
    
    is_pcs_on = last_written_m210 if last_written_m210 is not None else False
    
    if not is_pcs_on and hmi_on_pressed:
        print("HMI Mock Trigger: Turn ON PCS requested.")
        is_pcs_on = True
    elif is_pcs_on and hmi_off_pressed:
        print("HMI Mock Trigger: Turn OFF PCS requested.")
        is_pcs_on = False
    
    m210_val = is_pcs_on
    m211_val = not is_pcs_on
    client.write_coil(2258, m210_val)
    client.write_coil(2259, m211_val)
    last_written_m210 = m210_val
    last_written_m211 = m211_val
    
    current_set_power = last_active_power_val if last_active_power_val is not None else 0
    pcs_power_kw = float(current_set_power) if is_pcs_on else 0.0
    
    block1 = [
        to_u16(mock_freq * 100),
        to_u16(mock_v_rs * 10),
        to_u16(mock_v_st * 10),
        to_u16(mock_v_tr * 10)
    ]
    block2 = [
        to_u16(mock_v_r * 10),
        to_u16(mock_v_s * 10),
        to_u16(mock_v_t * 10)
    ]
    block3 = [
        to_u16(pcs_power_kw * 10),
        to_u16(-3) # Simulated reactive power (-0.3 kVar)
    ]
    block4 = [
        to_u16(34.8 * 10)
    ]
    
    client.write_registers(37068, block1)
    client.write_registers(37078, block2)
    client.write_registers(37093, block3)
    client.write_registers(37108, block4)
    client.write_registers(6206, [to_u16(current_set_power)])
    
    if is_pcs_on != last_printed_pcs_on or pcs_power_kw != last_printed_power:
        print(f"--> PLC WRITE SUCCESS: PCS State changed to On={is_pcs_on}, Power={pcs_power_kw}kW (Mock Telemetry Active)")
        last_printed_pcs_on = is_pcs_on
        last_printed_power = pcs_power_kw
        
    return is_pcs_on, pcs_power_kw


def poll_bms_telemetry(client, is_pcs_on, pcs_power_kw):
    """
    ดึงข้อมูล BMS จาก Gateway API หรือคำนวณข้อมูลจำลองและเขียนลง PLC D4600 - D4608
    (Fetch BMS telemetry and write to D4600 - D4608).
    - D4600: Total Voltage (÷10 V)
    - D4601: Total Current (÷10 A, signed)
    - D4602: SOC (%)
    - D4603: SOH (%)
    - D4604: Max Cell Voltage (mV)
    - D4605: Min Cell Voltage (mV)
    - D4606: Max Cell Temp (÷10 °C)
    - D4607: Min Cell Temp (÷10 °C)
    - D4608: Alarm Word
    """
    global BMS_API_URL
    try:
        req = urllib.request.Request(BMS_API_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=1.0) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            if res_data.get("success") and "data" in res_data and res_data["data"] is not None:
                data = res_data["data"]
                
                # Extract real BMS values from API
                voltage = float(data.get("voltage", 52.8))
                current = float(data.get("current", 0.0))
                soc = int(data.get("soc", 80))
                soh = int(data.get("soh", 100))
                max_cell_v = int(float(data.get("cellVoltageMax", 3.3)) * 1000) # V -> mV
                min_cell_v = int(float(data.get("cellVoltageMin", 3.3)) * 1000) # V -> mV
                max_cell_t = int(float(data.get("cellTempMax", 25.0)) * 10)     # °C -> 0.1°C
                min_cell_t = int(float(data.get("cellTempMin", 25.0)) * 10)     # °C -> 0.1°C
                
                alarm_word = 0
                if data.get("activeAlarms") and len(data["activeAlarms"]) > 0:
                    alarm_word = 1  # Standard alarm flag (can map bitwise word if needed)

                bms_block = [
                    to_u16(voltage * 10),
                    to_u16(current * 10),
                    to_u16(soc),
                    to_u16(soh),
                    to_u16(max_cell_v),
                    to_u16(min_cell_v),
                    to_u16(max_cell_t),
                    to_u16(min_cell_t),
                    to_u16(alarm_word)
                ]
                client.write_registers(37368, bms_block) # D4600 maps to 37368 (32768 + 4600)
                print(f"--> PLC WRITE SUCCESS: BMS Telemetry. SOC={soc}%, V={voltage:.1f}V, I={current:.1f}A")
                return
    except Exception as e:
        # If API is down or BMS is offline (returns NO_DATA), fall through to mock generator
        pass

    # --------------------------------------------------------------------------
    # FALLBACK MOCK DATA FOR BMS (ข้อมูลจำลองหากอุปกรณ์ออฟไลน์)
    # --------------------------------------------------------------------------
    # Mock SOC slowly drifts/fluctuates around 75%
    mock_soc = 75 + int(3.0 * math.sin(time.time() / 600.0))
    mock_soh = 99
    
    # Calculate mock current based on PCS active power (Current = Power / Voltage)
    # If PCS is discharging battery to grid (pcs_power_kw > 0), BMS current is discharging (negative)
    # If PCS is charging battery from grid (pcs_power_kw < 0), BMS current is charging (positive)
    if is_pcs_on and abs(pcs_power_kw) > 0:
        mock_current = - (pcs_power_kw * 1000.0 / 52.8) / 10.0 # scale to Ampere value divided by 10
    else:
        mock_current = 0.0 + random.uniform(-0.1, 0.1)

    mock_voltage = 52.8 + (mock_soc - 75) * 0.06
    mock_max_cell_v = 3310 + int(random.uniform(-5, 5))
    mock_min_cell_v = 3290 + int(random.uniform(-5, 5))
    mock_max_cell_t = 312 + int(random.uniform(-3, 3))
    mock_min_cell_t = 282 + int(random.uniform(-3, 3))
    mock_alarm = 0

    bms_block = [
        to_u16(mock_voltage * 10),
        to_u16(mock_current * 10),
        to_u16(mock_soc),
        to_u16(mock_soh),
        to_u16(mock_max_cell_v),
        to_u16(mock_min_cell_v),
        to_u16(mock_max_cell_t),
        to_u16(mock_min_cell_t),
        to_u16(mock_alarm)
    ]
    client.write_registers(37368, bms_block)
    print(f"--> PLC WRITE SUCCESS: BMS Telemetry (MOCK). SOC={mock_soc}%, V={mock_voltage:.1f}V, I={mock_current:.1f}A")


def poll_inverter_telemetry(client):
    """
    ดึงข้อมูล Inverter จาก Gateway API หรือคำนวณข้อมูลจำลองและเขียนลง PLC D4500 - D4512
    (Fetch Inverter telemetry and write to D4500 - D4512).
    - D4500: Inverter Active Power (kW, unsigned)
    - D4501: Inverter Reactive Power (kVar, signed)
    - D4502: Inverter Daily Generation (kWh, ÷10)
    - D4503, D4504: Inverter Total Generation (kWh, 32-bit)
    - D4505: Voltage R (÷10 V)
    - D4506: Voltage S (÷10 V)
    - D4507: Voltage T (÷10 V)
    - D4508: Current R (÷10 A)
    - D4509: Current S (÷10 A)
    - D4510: Current T (÷10 A)
    - D4511: Cabinet Temperature (÷10 °C)
    - D4512: Inverter Status Code (0=Off, 1=Standby, 2=Running, 3=Fault)
    """
    global INVERTER_API_URL, mock_inv_total_gen, mock_inv_daily_gen, last_date_day
    
    # Handle daily reset at midnight (รีเซ็ตค่าพลังงานรายวันเมื่อขึ้นวันใหม่)
    now = datetime.now()
    if last_date_day is None:
        last_date_day = now.day
    elif last_date_day != now.day:
        mock_inv_daily_gen = 0.0
        last_date_day = now.day

    try:
        req = urllib.request.Request(INVERTER_API_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=1.0) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            if res_data.get("success") and "data" in res_data and res_data["data"] is not None:
                data = res_data["data"]
                
                # Extract real Inverter values
                act_p = float(data.get("activePower", 0.0))
                react_p = float(data.get("reactivePower", 0.0))
                daily_gen = float(data.get("dailyGeneration", 0.0))
                total_gen = float(data.get("totalGeneration", 0.0))
                v_r = float(data.get("voltageR", 230.0))
                v_s = float(data.get("voltageS", 230.0))
                v_t = float(data.get("voltageT", 230.0))
                i_r = float(data.get("currentR", 0.0))
                i_s = float(data.get("currentS", 0.0))
                i_t = float(data.get("currentT", 0.0))
                temp = float(data.get("cabinetTemp", 25.0))
                status = int(data.get("status", 0))

                total_gen_words = to_u32_words(total_gen)

                inv_block = [
                    to_u16(act_p),
                    to_u16(react_p),
                    to_u16(daily_gen * 10.0),
                    total_gen_words[0],
                    total_gen_words[1],
                    to_u16(v_r * 10.0),
                    to_u16(v_s * 10.0),
                    to_u16(v_t * 10.0),
                    to_u16(i_r * 10.0),
                    to_u16(i_s * 10.0),
                    to_u16(i_t * 10.0),
                    to_u16(temp * 10.0),
                    to_u16(status)
                ]
                client.write_registers(37268, inv_block) # D4500 maps to 37268 (32768 + 4500)
                print(f"--> PLC WRITE SUCCESS: Inverter Telemetry. Power={act_p:.1f}kW, Daily={daily_gen:.2f}kWh")
                return
    except Exception as e:
        # Fallback to mock on connection failure
        pass

    # --------------------------------------------------------------------------
    # FALLBACK MOCK DATA FOR INVERTER (ข้อมูลจำลองหากอุปกรณ์ออฟไลน์ - จำลองโค้งแสงแดด)
    # --------------------------------------------------------------------------
    hour = now.hour
    
    # Solar Output Bell Curve: Peak at 12:00 PM (100 kW max)
    if 6 <= hour <= 18:
        # Angle from 0 to Pi over 12 hours of daylight
        day_progress = (hour - 6) + (now.minute / 60.0)
        rad = math.pi * day_progress / 12.0
        mock_active_power = 100.0 * math.sin(rad) * random.uniform(0.96, 1.04)
        mock_status = 2  # Running/Generating
    else:
        mock_active_power = 0.0
        mock_status = 0  # Off (Nighttime)

    # Accumulate simulated solar energy over time (1s steps)
    energy_increment = (mock_active_power / 3600.0) * 1.0
    mock_inv_daily_gen += energy_increment
    mock_inv_total_gen += energy_increment

    mock_v_r = 230.1 + random.uniform(-0.6, 0.6)
    mock_v_s = 229.7 + random.uniform(-0.6, 0.6)
    mock_v_t = 230.4 + random.uniform(-0.6, 0.6)

    # Calculate mock line current: I = P / (3 * V_phase) * 1000
    if mock_active_power > 0:
        mock_i_r = (mock_active_power * 1000.0 / (3.0 * mock_v_r)) + random.uniform(-0.1, 0.1)
        mock_i_s = (mock_active_power * 1000.0 / (3.0 * mock_v_s)) + random.uniform(-0.1, 0.1)
        mock_i_t = (mock_active_power * 1000.0 / (3.0 * mock_v_t)) + random.uniform(-0.1, 0.1)
    else:
        mock_i_r = mock_i_s = mock_i_t = 0.0

    mock_reactive_power = 0.0 + random.uniform(-0.2, 0.2)
    mock_temp = 25.0 + (mock_active_power / 100.0) * 18.0 + random.uniform(-0.3, 0.3)

    total_gen_words = to_u32_words(mock_inv_total_gen)

    inv_block = [
        to_u16(mock_active_power),
        to_u16(mock_reactive_power),
        to_u16(mock_inv_daily_gen * 10.0),
        total_gen_words[0],
        total_gen_words[1],
        to_u16(mock_v_r * 10.0),
        to_u16(mock_v_s * 10.0),
        to_u16(mock_v_t * 10.0),
        to_u16(mock_i_r * 10.0),
        to_u16(mock_i_s * 10.0),
        to_u16(mock_i_t * 10.0),
        to_u16(mock_temp * 10.0),
        to_u16(mock_status)
    ]
    client.write_registers(37268, inv_block)
    print(f"--> PLC WRITE SUCCESS: Inverter Telemetry (MOCK). Power={mock_active_power:.1f}kW, Daily={mock_inv_daily_gen:.2f}kWh, Status={mock_status}")


def poll_powermeter_telemetry(client, is_pcs_on, pcs_power_kw):
    """
    ดึงข้อมูล Powermeter จาก Gateway API หรือคำนวณข้อมูลจำลองและเขียนลง PLC D4400 - D4414
    (Fetch Power Meter telemetry and write to D4400 - D4414).
    - D4400: Grid Active Power (kW, signed)
    - D4401: Grid Reactive Power (kVar, signed)
    - D4402: Grid Apparent Power (kVA)
    - D4403: Grid Power Factor (x1000)
    - D4404: Grid Frequency (Hz * 100)
    - D4405: Grid Voltage R-S (÷10 V)
    - D4406: Grid Voltage S-T (÷10 V)
    - D4407: Grid Voltage T-R (÷10 V)
    - D4408: Grid Current R (÷10 A)
    - D4409: Grid Current S (÷10 A)
    - D4410: Grid Current T (÷10 A)
    - D4411, D4412: Total Import Energy (kWh, 32-bit)
    - D4413, D4414: Total Export Energy (kWh, 32-bit)
    """
    global METER_API_URL, mock_import_energy, mock_export_energy
    try:
        req = urllib.request.Request(METER_API_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=1.0) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            if res_data.get("success") and "data" in res_data and res_data["data"] is not None:
                data = res_data["data"]
                
                # Extract real Powermeter values
                act_p = float(data.get("activePower", 0.0))
                react_p = float(data.get("reactivePower", 0.0))
                app_p = float(data.get("apparentPower", 0.0))
                pf = float(data.get("powerFactor", 1.0))
                freq = float(data.get("frequency", 50.0))
                v_rs = float(data.get("voltageRS", 400.0))
                v_st = float(data.get("voltageST", 400.0))
                v_tr = float(data.get("voltageTR", 400.0))
                i_r = float(data.get("currentR", 0.0))
                i_s = float(data.get("currentS", 0.0))
                i_t = float(data.get("currentT", 0.0))
                import_e = float(data.get("importEnergy", 0.0))
                export_e = float(data.get("exportEnergy", 0.0))

                import_words = to_u32_words(import_e)
                export_words = to_u32_words(export_e)

                meter_block = [
                    to_u16(act_p),
                    to_u16(react_p),
                    to_u16(app_p),
                    to_u16(pf * 1000.0),
                    to_u16(freq * 100.0),
                    to_u16(v_rs * 10.0),
                    to_u16(v_st * 10.0),
                    to_u16(v_tr * 10.0),
                    to_u16(i_r * 10.0),
                    to_u16(i_s * 10.0),
                    to_u16(i_t * 10.0),
                    import_words[0],
                    import_words[1],
                    export_words[0],
                    export_words[1]
                ]
                client.write_registers(37168, meter_block) # D4400 maps to 37168 (32768 + 4400)
                print(f"--> PLC WRITE SUCCESS: Meter Telemetry. Grid={act_p:.1f}kW, Import={import_e:.1f}kWh")
                return
    except Exception as e:
        # Fallback to mock on connection failure
        pass

    # --------------------------------------------------------------------------
    # FALLBACK MOCK DATA FOR METER (ข้อมูลจำลองหากอุปกรณ์ออฟไลน์ - คำนวณโหลดอาคารรวมกับแบตเตอรี่)
    # --------------------------------------------------------------------------
    # Base building power usage: 35 kW + noise
    building_load = 35.0 + random.uniform(-3.0, 3.0)
    
    # Grid Active Power = Building Load - Battery Power Output (pcs_power_kw)
    # If PCS is discharging battery to Grid (pcs_power_kw > 0), grid import power reduces
    # If PCS is charging battery from Grid (pcs_power_kw < 0), grid import power increases (pcs_power_kw is negative, so building_load - (-pcs_power_kw) = load + charge)
    mock_active_power = building_load - pcs_power_kw
    mock_reactive_power = 2.8 + random.uniform(-0.3, 0.3)
    
    # Calculate apparent power and power factor
    mock_apparent_power = math.sqrt(mock_active_power**2 + mock_reactive_power**2)
    mock_pf = 1.0 if mock_apparent_power == 0 else abs(mock_active_power / mock_apparent_power)
    
    mock_freq = 50.0 + random.uniform(-0.02, 0.02)
    mock_v_rs = 398.8 + random.uniform(-0.8, 0.8)
    mock_v_st = 398.1 + random.uniform(-0.8, 0.8)
    mock_v_tr = 399.5 + random.uniform(-0.8, 0.8)

    # Line currents: I = Apparent Power * 1000 / (sqrt(3) * Line_Voltage_Avg)
    v_avg = (mock_v_rs + mock_v_st + mock_v_tr) / 3.0
    i_avg = (mock_apparent_power * 1000.0) / (math.sqrt(3.0) * v_avg) if v_avg > 0 else 0.0
    mock_i_r = i_avg + random.uniform(-0.3, 0.3)
    mock_i_s = i_avg + random.uniform(-0.3, 0.3)
    mock_i_t = i_avg + random.uniform(-0.3, 0.3)

    # Accumulate simulated energies
    energy_increment = (mock_active_power / 3600.0) * 1.0
    if mock_active_power > 0:
        mock_import_energy += energy_increment
    else:
        mock_export_energy += abs(energy_increment)

    import_words = to_u32_words(mock_import_energy)
    export_words = to_u32_words(mock_export_energy)

    meter_block = [
        to_u16(mock_active_power),
        to_u16(mock_reactive_power),
        to_u16(mock_apparent_power),
        to_u16(mock_pf * 1000.0),
        to_u16(mock_freq * 100.0),
        to_u16(mock_v_rs * 10.0),
        to_u16(mock_v_st * 10.0),
        to_u16(mock_v_tr * 10.0),
        to_u16(mock_i_r * 10.0),
        to_u16(mock_i_s * 10.0),
        to_u16(mock_i_t * 10.0),
        import_words[0],
        import_words[1],
        export_words[0],
        export_words[1]
    ]
    client.write_registers(37168, meter_block)
    print(f"--> PLC WRITE SUCCESS: Meter Telemetry (MOCK). Grid={mock_active_power:.1f}kW, Import={mock_import_energy:.2f}kWh")


# ==============================================================================
# MAIN EXECUTION LOOP (ลูปหลักการทำงาน)
# ==============================================================================
client = ModbusTcpClient(PLC_IP, port=502)

if client.connect():
    print(f"CONNECTED TO PLC AT {PLC_IP}")

    while True:
        # --- Connection Watchdog ---
        if not client.connected:
            print("PLC Connection lost. Reconnecting...")
            if not client.connect():
                print("Reconnection failed. Waiting...")
                time.sleep(2)
                continue
            else:
                print("RECONNECTED TO PLC")

        # --- 1. Read HMI Button Overrides ---
        hmi_on_pressed = False
        hmi_off_pressed = False
        hmi_set_power_pressed = False
        
        try:
            # Read M210 - M211 (Coils 2258 and 2259) to see if user pressed ON or OFF
            coils_res = client.read_coils(2258, count=2)
            if coils_res is not None and not coils_res.isError():
                raw_on = coils_res.bits[0]
                raw_off = coils_res.bits[1]
                
                # Check rising edge
                if last_written_m210 is None:
                    hmi_on_pressed = raw_on
                else:
                    hmi_on_pressed = raw_on and not last_written_m210
                    
                if last_written_m211 is None:
                    hmi_off_pressed = raw_off
                else:
                    hmi_off_pressed = raw_off and not last_written_m211
        except Exception as e:
            print(f"Failed to read ON/OFF coils: {e}")

        try:
            # Read M230 (Coil 2278) to see if user pressed SET power
            m230_res = client.read_coils(2278, count=1)
            if m230_res is not None and not m230_res.isError():
                hmi_set_power_pressed = m230_res.bits[0]
        except Exception as e:
            print(f"Failed to read M230 coil: {e}")

        # Process SET power button click from HMI
        if hmi_set_power_pressed:
            try:
                reg_res = client.read_holding_registers(6206, count=1)  # D2110 is 6206 (4096 + 2110)
                if reg_res is not None and not reg_res.isError():
                    raw_val = reg_res.registers[0]
                    power_val = to_s16(raw_val) * 10  # Scale back to kW * 10
                    print(f"HMI Trigger: Set Power (M230 is True). Raw D2110={raw_val}, Scale={power_val} W ({(power_val/10.0):.1f} kW)")
                    if send_active_power_command(power_val):
                        client.write_coil(2278, False)  # Reset M230
                        last_active_power_val = to_s16(raw_val)
                        print("HMI Trigger: Reset M230 to False")
            except Exception as e:
                print(f"Error handling Active Power command: {e}")

        # --- 2. Write PC Time to PLC D200-D205 ---
        now = datetime.now()
        time_values = [
            to_u16(now.hour),
            to_u16(now.minute),
            to_u16(now.second),
            to_u16(now.day),
            to_u16(now.month),
            to_u16(now.year)
        ]
        try:
            client.write_registers(4296, time_values)  # D200 is 4296 (4096 + 200)
            if now.second == 0:
                print(f"PLC Heartbeat: Sync Time ({now.hour:02d}:{now.minute:02d})")
        except Exception as e:
            print(f"Failed to write time registers: {e}")

        # --- 3. Telemetry Sync Block (BMS, PCS, Inverter, Powermeter) ---
        # A. PCS Telemetry (D4300 - D4340)
        is_pcs_on, pcs_power_kw = poll_pcs_telemetry(
            client, hmi_on_pressed, hmi_off_pressed, hmi_set_power_pressed
        )
        
        # B. BMS Telemetry (D4600 - D4608)
        poll_bms_telemetry(client, is_pcs_on, pcs_power_kw)
        
        # C. Inverter Telemetry (D4500 - D4512)
        poll_inverter_telemetry(client)
        
        # D. Power Meter Telemetry (D4400 - D4414)
        poll_powermeter_telemetry(client, is_pcs_on, pcs_power_kw)

        # --- 4. Today's Active Schedule Sync (Every 5 minutes) ---
        current_epoch = time.time()
        if current_epoch - last_poll_active_schedule_time > 300 or last_poll_active_schedule_time == 0:
            last_poll_active_schedule_time = current_epoch
            try:
                today_sch = fetch_today_schedule()
                if today_sch:
                    client.write_coil(2298, False)  # M250 (Holiday) = False
                    client.write_coil(2300, today_sch.get("chargeEnabled", False))   # M252
                    client.write_coil(2301, today_sch.get("dischargeEnabled", False))# M253
                    
                    chg_start_h, chg_start_m = map(int, today_sch.get("chargeStart", "22:00").split(':'))
                    chg_end_h, chg_end_m = map(int, today_sch.get("chargeEnd", "08:00").split(':'))
                    chg_power = today_sch.get("chargePowerKw", 0)
                    
                    dis_start_h, dis_start_m = map(int, today_sch.get("dischargeStart", "09:00").split(':'))
                    dis_end_h, dis_end_m = map(int, today_sch.get("dischargeEnd", "18:00").split(':'))
                    dis_power = today_sch.get("dischargePowerKw", 0)
                    
                    today_vals = [
                        to_u16(chg_start_h), to_u16(chg_start_m), to_u16(chg_end_h), to_u16(chg_end_m), to_u16(chg_power),
                        0, 0, 0, 0, 0,
                        to_u16(dis_start_h), to_u16(dis_start_m), to_u16(dis_end_h), to_u16(dis_end_m), to_u16(dis_power)
                    ]
                    client.write_registers(4306, today_vals)  # D210 maps to 4306 (4096 + 210)
                    print(f"--> PLC WRITE SUCCESS: Synced Today's Active Schedule (D210-D224): CHG={today_sch.get('chargeEnabled')}, DISCHG={today_sch.get('dischargeEnabled')}")
            except Exception as e:
                print(f"Failed to sync active today's schedule: {e}")

        # --- 5. Calendar Grid & Details Sync (HMI Grid clicks) ---
        try:
            # Read Viewed Month/Year (D260-D261), Selected Day Charge (D265) -> read 10 registers from D260 (4356)
            hmi_inputs_chg = client.read_holding_registers(4356, count=10)
            # Read Selected Day Discharge (D365) -> read 10 registers from D360 (4456)
            hmi_inputs_dis = client.read_holding_registers(4456, count=10)
            
            if (hmi_inputs_chg is not None and not hmi_inputs_chg.isError() and 
                hmi_inputs_dis is not None and not hmi_inputs_dis.isError()):
                
                hmi_month = hmi_inputs_chg.registers[0] # D260
                hmi_year = hmi_inputs_chg.registers[1]   # D261
                active_hmi_day_chg = hmi_inputs_chg.registers[5]  # D265
                active_hmi_day_dis = hmi_inputs_dis.registers[5]  # D365
                
                # Check if viewed month/year changed or time to periodic poll (every 10 seconds)
                should_refresh_calendar = (
                    (0 <= hmi_month <= 11 and hmi_year > 0) and (
                        hmi_month != last_hmi_month_chg or 
                        hmi_year != last_hmi_year_chg or 
                        current_epoch - last_hmi_calendar_poll_time_chg > 10
                    )
                )
                
                if should_refresh_calendar:
                    last_hmi_calendar_poll_time_chg = current_epoch
                    
                    # Update M29-M31 coils based on number of days in the month (hide invalid dates on HMI)
                    _, num_days = calendar.monthrange(hmi_year, hmi_month + 1)
                    m29_exists = num_days >= 29
                    m30_exists = num_days >= 30
                    m31_exists = num_days >= 31
                    client.write_coils(2077, [m29_exists, m30_exists, m31_exists]) # M29 maps to 2077
                    print(f"--> PLC WRITE SUCCESS: Month has {num_days} days. Updated M29-M31 visibility: M29={m29_exists}, M30={m30_exists}, M31={m31_exists}")
                    
                    calendar_data = fetch_month_schedule(hmi_year, hmi_month + 1)
                    if calendar_data:
                        # Update Charge statuses (D270 - D300)
                        statuses_chg = []
                        for day_idx in range(31):
                            if day_idx < len(calendar_data):
                                day_sch = calendar_data[day_idx]
                                if day_sch.get("chargeEnabled"):
                                    if active_hmi_day_chg == day_idx + 1:
                                        statuses_chg.append(to_u16(0))  # Selected/Active
                                    else:
                                        statuses_chg.append(to_u16(1))  # Scheduled
                                else:
                                    statuses_chg.append(to_u16(2))      # Standby
                            else:
                                statuses_chg.append(to_u16(2))
                        
                        if statuses_chg != last_hmi_statuses_chg or hmi_month != last_hmi_month_chg or hmi_year != last_hmi_year_chg:
                            client.write_registers(4366, statuses_chg)  # D270
                            last_hmi_statuses_chg = statuses_chg
                            print(f"--> PLC WRITE SUCCESS: Loaded Month {hmi_month+1}/{hmi_year} Charge Statuses (D270-D300)")

                        # Update Discharge statuses (D370 - D400)
                        statuses_dis = []
                        for day_idx in range(31):
                            if day_idx < len(calendar_data):
                                day_sch = calendar_data[day_idx]
                                if day_sch.get("dischargeEnabled"):
                                    if active_hmi_day_dis == day_idx + 1:
                                        statuses_dis.append(to_u16(0))  # Selected/Active
                                    else:
                                        statuses_dis.append(to_u16(1))  # Scheduled
                                else:
                                    statuses_dis.append(to_u16(2))      # Standby
                            else:
                                statuses_dis.append(to_u16(2))
                                
                        if statuses_dis != last_hmi_statuses_dis or hmi_month != last_hmi_month_chg or hmi_year != last_hmi_year_chg:
                            client.write_registers(4466, statuses_dis)  # D370
                            last_hmi_statuses_dis = statuses_dis
                            print(f"--> PLC WRITE SUCCESS: Loaded Month {hmi_month+1}/{hmi_year} Discharge Statuses (D370-D400)")
                            
                    last_hmi_month_chg = hmi_month
                    last_hmi_year_chg = hmi_year

                # Load Selected Day's detail schedule to shared registers (D310 - D324)
                target_day = 0
                is_charge_view = True
                
                if active_hmi_day_chg > 0:
                    target_day = active_hmi_day_chg
                    is_charge_view = True
                elif active_hmi_day_dis > 0:
                    target_day = active_hmi_day_dis
                    is_charge_view = False
                    
                if target_day > 0:
                    target_month = hmi_month
                    target_year = hmi_year
                    calendar_data = fetch_month_schedule(target_year, target_month + 1)
                    if calendar_data and target_day <= len(calendar_data):
                        day_sch = calendar_data[target_day - 1]
                        
                        chg_en = day_sch.get("chargeEnabled", False)
                        chg_start = day_sch.get("chargeStart", "22:00")
                        chg_end = day_sch.get("chargeEnd", "08:00")
                        chg_pow = day_sch.get("chargePowerKw", 0) if chg_en else 0
                        
                        dis_en = day_sch.get("dischargeEnabled", False)
                        dis_start = day_sch.get("dischargeStart", "09:00")
                        dis_end = day_sch.get("dischargeEnd", "18:00")
                        dis_pow = day_sch.get("dischargePowerKw", 0) if dis_en else 0
                        
                        chg_h, chg_m = map(int, chg_start.split(':'))
                        chg_eh, chg_em = map(int, chg_end.split(':'))
                        dis_h, dis_m = map(int, dis_start.split(':'))
                        dis_eh, dis_em = map(int, dis_end.split(':'))
                        
                        detail_vals = [
                            to_u16(chg_h), to_u16(chg_m), to_u16(chg_eh), to_u16(chg_em), to_u16(chg_pow),
                            0, 0, 0, 0, 0, # padding
                            to_u16(dis_h), to_u16(dis_m), to_u16(dis_eh), to_u16(dis_em), to_u16(dis_pow),
                            to_u16(0)      # D325 Holiday Indicator (always 0)
                        ]
                        
                        if detail_vals != last_detail_vals:
                            client.write_registers(4406, detail_vals)  # D310 starts at 4406 (4096 + 310)
                            last_detail_vals = detail_vals
                            mode_str = "Charge" if is_charge_view else "Discharge"
                            print(f"--> PLC WRITE SUCCESS: Updated Day {target_day} ({mode_str}) details in D310-D324")
                else:
                    # Clear details panel registers if no day selected
                    clear_vals = [0] * 16
                    if clear_vals != last_detail_vals:
                        client.write_registers(4406, clear_vals)
                        last_detail_vals = clear_vals
                        print("--> PLC WRITE SUCCESS: Cleared calendar detail registers D310-D324")
        except Exception as e:
            print(f"HMI Calendar & Detail Sync Error: {e}")

        # Sleep for 1 second before the next execution cycle
        time.sleep(1)

else:
    print(f"FAILED TO CONNECT TO PLC AT {PLC_IP}")