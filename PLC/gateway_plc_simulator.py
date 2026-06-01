import json
from pymodbus.client import ModbusTcpClient
from datetime import datetime
import time
import random

PLC_IP = "192.168.0.5"

# Mock state variables
is_pcs_on = False
pcs_set_power_kw = 0
last_written_m210 = None
last_written_m211 = None

# Calendar & HMI Sync variables
last_hmi_month = None
last_hmi_year = None
active_hmi_day = 0  # 0 means panel is closed

# Local Mock Database for Schedules (Generates mock schedules dynamically)
def get_mock_day_schedule(year, month, day):
    """
    Generate deterministic mock schedules for testing:
    - Even days: Active with CHG/DISCHG plans.
    - Odd days: Standby (disabled).
    """
    date_str = f"{year:04d}-{month:02d}-{day:02d}"
    if day % 2 == 0:
        # Active day (Even day numbers)
        return {
            "date": date_str,
            "chargeEnabled": True,
            "chargeStart": "22:00",
            "chargeEnd": "08:00",
            "chargePowerKw": 50 + (day % 3) * 10,
            "dischargeEnabled": True,
            "dischargeStart": "09:00",
            "dischargeEnd": "18:00",
            "dischargePowerKw": 80 + (day % 4) * 10
        }
    else:
        # Standby day (Odd day numbers)
        return {
            "date": date_str,
            "chargeEnabled": False,
            "chargeStart": "22:00",
            "chargeEnd": "08:00",
            "chargePowerKw": 0,
            "dischargeEnabled": False,
            "dischargeStart": "09:00",
            "dischargeEnd": "18:00",
            "dischargePowerKw": 0
        }

def get_mock_month_schedule(year, month):
    """Generate 31-day mock schedule for the month."""
    import calendar
    _, num_days = calendar.monthrange(year, month)
    # We always return 31 entries to prevent array out of bound on PLC side
    schedules = []
    for d in range(1, 32):
        schedules.append(get_mock_day_schedule(year, month, d))
    return schedules

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

client = ModbusTcpClient(PLC_IP, port=502)

if client.connect():
    print(f"CONNECTED TO PLC SIMULATOR/DEVICE AT {PLC_IP}")
    print("MOCK SYSTEM IN PROGRESS... RUNNING WITHOUT DOCKER & DATABASE")

    while True:
        # --- 0. Connection Watchdog ---
        if not client.connected:
            print("PLC Connection lost. Reconnecting...")
            if not client.connect():
                print("Reconnection failed. Waiting...")
                time.sleep(2)
                continue
            else:
                print("RECONNECTED TO PLC")

        # --- 0.1 Read Coils from PLC to check for HMI commands ---
        hmi_on_pressed = False
        hmi_off_pressed = False
        hmi_set_power_pressed = False
        try:
            coils_res = client.read_coils(2258, count=2)
            if coils_res is not None and not coils_res.isError():
                raw_on = coils_res.bits[0]
                raw_off = coils_res.bits[1]
                
                # Detect rising edge trigger from HMI
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
            m230_res = client.read_coils(2278, count=1)
            if m230_res is not None and not m230_res.isError():
                hmi_set_power_pressed = m230_res.bits[0]
        except Exception as e:
            print(f"Failed to read M230 coil: {e}")

        # --- 0.2 If SET power button is pressed, process active power ---
        if hmi_set_power_pressed:
            try:
                reg_res = client.read_holding_registers(6206, count=1)
                if reg_res is not None and not reg_res.isError():
                    raw_val = reg_res.registers[0]
                    # HMI writes power in kW (signed)
                    pcs_set_power_kw = to_s16(raw_val)
                    print(f"HMI Mock Trigger: Set Active Power requested (M230). New Mock Target = {pcs_set_power_kw} kW")
                    # Clear M230 trigger coil
                    client.write_coil(2278, False)
                else:
                    print("HMI Mock Trigger: Failed to read register D2110 (6206)")
            except Exception as e:
                print(f"HMI Mock Trigger: Error setting Active Power: {e}")

        # --- 1. Write current PC time to D200-D205 ---
        now = datetime.now()
        hour, minute, second = now.hour, now.minute, now.second
        day, month, year = now.day, now.month, now.year

        time_values = [
            to_u16(hour), to_u16(minute), to_u16(second),
            to_u16(day), to_u16(month), to_u16(year)
        ]

        try:
            client.write_registers(4296, time_values)  # D200
        except Exception as e:
            print(f"Failed to write time to PLC: {e}")

        # --- 2. Generate and Write Mock PCS Data to D4300+ ---
        try:
            # 2.1 ON/OFF Control Simulation
            if not is_pcs_on and hmi_on_pressed:
                print("HMI Mock Trigger: Turn ON PCS requested. Setting PCS State to ON.")
                is_pcs_on = True
                hmi_on_pressed = False
            elif is_pcs_on and hmi_off_pressed:
                print("HMI Mock Trigger: Turn OFF PCS requested. Setting PCS State to STANDBY.")
                is_pcs_on = False
                hmi_off_pressed = False

            # Update coils M210 & M211 back to HMI
            client.write_coil(2258, is_pcs_on)      # M210 (ON indicator)
            client.write_coil(2259, not is_pcs_on)  # M211 (OFF indicator)
            last_written_m210 = is_pcs_on
            last_written_m211 = not is_pcs_on

            # 2.2 Generate Mock Meter Telemetry
            # Add small random noise to simulate fluctuating actual values
            mock_freq = 50.0 + random.uniform(-0.03, 0.03)
            # Grid voltages
            mock_v_rs = 398.0 + random.uniform(-1.5, 1.5)
            mock_v_st = 397.0 + random.uniform(-1.5, 1.5)
            mock_v_tr = 397.5 + random.uniform(-1.5, 1.5)
            # Phase voltages
            mock_v_r = 229.5 + random.uniform(-1.0, 1.0)
            mock_v_s = 230.2 + random.uniform(-1.0, 1.0)
            mock_v_t = 229.8 + random.uniform(-1.0, 1.0)

            # Active Power Simulation:
            # If PCS is ON, actual power floats around the set value (pcs_set_power_kw).
            # If PCS is OFF/Standby, power is 0 kW.
            if is_pcs_on:
                mock_active_power = pcs_set_power_kw + random.uniform(-0.5, 0.5)
            else:
                mock_active_power = 0.0

            mock_reactive_power = -0.3 + random.uniform(-0.1, 0.1)
            mock_temp = 34.5 + random.uniform(-0.2, 0.2)

            # Block 1: D4300 - D4303 (Grid Freq, V RS, V ST, V TR)
            # Scale Freq * 100, Voltage * 10
            block1 = [
                to_u16(mock_freq * 100),
                to_u16(mock_v_rs * 10),
                to_u16(mock_v_st * 10),
                to_u16(mock_v_tr * 10)
            ]
            # Block 2: D4310 - D4312 (AC V R, AC V S, AC V T)
            block2 = [
                to_u16(mock_v_r * 10),
                to_u16(mock_v_s * 10),
                to_u16(mock_v_t * 10)
            ]
            # Block 3: D4325 - D4326 (Active Power, Reactive Power)
            # PCS Register scale is: Active power (0.1 kW), Reactive Power (0.1 kVar)
            # Note: Active power is stored directly scaled in PLC
            block3 = [
                to_u16(mock_active_power * 10),
                to_u16(mock_reactive_power * 10)
            ]
            # Block 4: D4340 (Ambient Temp)
            block4 = [
                to_u16(mock_temp * 10)
            ]

            # Write blocks to PLC
            client.write_registers(37068, block1)  # D4300
            client.write_registers(37078, block2)  # D4310
            client.write_registers(37093, block3)  # D4325
            client.write_registers(37108, block4)  # D4340

            # Keep HMI D2110 input synced with mock set power value
            client.write_registers(6206, [to_u16(pcs_set_power_kw)])

            print(f"MOCK PCS DATA: On={is_pcs_on}, Set={pcs_set_power_kw}kW, Act={mock_active_power:.1f}kW, Freq={mock_freq:.2f}Hz")
        except Exception as e:
            print(f"Failed to write mock PCS telemetry: {e}")

        # --- 3. Sync Today's Active Mock Schedule to PLC (M252, M253, D210-D224) ---
        try:
            # We mock today's schedule based on current PC date
            today_sch = get_mock_day_schedule(year, month, day)
            
            client.write_coil(2298, False)  # M250 (Holiday is always False)
            client.write_coil(2300, today_sch.get("chargeEnabled", False))
            client.write_coil(2301, today_sch.get("dischargeEnabled", False))
            
            chg_start_h, chg_start_m = map(int, today_sch.get("chargeStart", "22:00").split(':'))
            chg_end_h, chg_end_m = map(int, today_sch.get("chargeEnd", "08:00").split(':'))
            chg_power = today_sch.get("chargePowerKw", 0)
            
            dis_start_h, dis_start_m = map(int, today_sch.get("dischargeStart", "09:00").split(':'))
            dis_end_h, dis_end_m = map(int, today_sch.get("dischargeEnd", "18:00").split(':'))
            dis_power = today_sch.get("dischargePowerKw", 0)
            
            today_vals = [
                to_u16(chg_start_h), to_u16(chg_start_m), to_u16(chg_end_h), to_u16(chg_end_m), to_u16(chg_power),
                0, 0, 0, 0, 0, # padding
                to_u16(dis_start_h), to_u16(dis_start_m), to_u16(dis_end_h), to_u16(dis_end_m), to_u16(dis_power)
            ]
            client.write_registers(4306, today_vals)  # D210
        except Exception as e:
            print(f"Failed to sync today's active mock schedule: {e}")

        # --- 4. HMI Calendar Navigation & Day Detail Panel Sync ---
        try:
            # Read Viewed Month/Year (D260-D261) and Selected Day (D262)
            # Address: 4356, count=3
            hmi_inputs = client.read_holding_registers(4356, count=3)
            if hmi_inputs is not None and not hmi_inputs.isError():
                hmi_month = hmi_inputs.registers[0]
                hmi_year = hmi_inputs.registers[1]
                hmi_day_click = hmi_inputs.registers[2]
                
                # A. Month/Year Navigation: Generate mock statuses for 31 days
                if 0 <= hmi_month <= 11 and hmi_year > 0 and (hmi_month != last_hmi_month or hmi_year != last_hmi_year):
                    print(f"HMI Calendar: Month/Year navigation triggered. HMI raw Month={hmi_month}, Year={hmi_year}")
                    calendar_data = get_mock_month_schedule(hmi_year, hmi_month + 1)
                    
                    # Write 31-day statuses (D270 - D300) -> 4366 to 4396
                    statuses = []
                    for day_idx in range(31):
                        day_sch = calendar_data[day_idx]
                        if day_sch.get("chargeEnabled") or day_sch.get("dischargeEnabled"):
                            statuses.append(to_u16(0))  # Active Day (Green)
                        else:
                            statuses.append(to_u16(2))  # Standby Day (Grey/Off)
                    
                    client.write_registers(4366, statuses)
                    last_hmi_month = hmi_month
                    last_hmi_year = hmi_year
                    print("HMI Calendar: Successfully wrote 31-day mock statuses to D270-D300")
                
                # B. Day Click Toggle Handshake
                if hmi_day_click > 0:
                    # Check current M300 (Visibility): 2348
                    m300_res = client.read_coils(2348, count=1)
                    is_panel_open = m300_res.bits[0] if (m300_res is not None and not m300_res.isError()) else False
                    
                    if hmi_day_click == active_hmi_day and is_panel_open:
                        # 1. Pressed the same day again -> CLOSE PANEL
                        print(f"HMI Calendar: Day {hmi_day_click} pressed again. Closing detail panel...")
                        client.write_coil(2348, False)  # M300 = OFF
                        client.write_registers(4359, [0])  # D263 = 0
                        active_hmi_day = 0
                    else:
                        # 2. Pressed a new day or panel was closed -> LOAD DETAILS & OPEN PANEL
                        print(f"HMI Calendar: Day {hmi_day_click} selected. Loading mock details...")
                        target_month = hmi_month if 0 <= hmi_month <= 11 else (last_hmi_month or 0)
                        target_year = hmi_year if hmi_year > 0 else (last_hmi_year or 2026)
                        
                        day_sch = get_mock_day_schedule(target_year, target_month + 1, hmi_day_click)
                        
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
                        
                        # Write details to D310 - D325 (Address: 4406)
                        detail_vals = [
                            to_u16(chg_h), to_u16(chg_m), to_u16(chg_eh), to_u16(chg_em), to_u16(chg_pow),
                            0, 0, 0, 0, 0, # padding
                            to_u16(dis_h), to_u16(dis_m), to_u16(dis_eh), to_u16(dis_em), to_u16(dis_pow),
                            to_u16(0)  # D325 (always 0, holiday removed)
                        ]
                        client.write_registers(4406, detail_vals)
                        
                        # Write display day (D263 = hmi_day_click)
                        client.write_registers(4359, [to_u16(hmi_day_click)])
                        
                        # Show panel (M300 = ON)
                        client.write_coil(2348, True)
                        active_hmi_day = hmi_day_click
                        print(f"HMI Calendar: Displaying mock details for Day {hmi_day_click}")
                    
                    # 3. Always reset input day click (D262 = 0)
                    client.write_registers(4358, [0])
        except Exception as e:
            print(f"HMI Calendar Sync Error: {e}")

        # Cycle loop every 1 second
        time.sleep(1)

else:
    print(f"CONNECT TO PLC FAILED AT {PLC_IP}")
