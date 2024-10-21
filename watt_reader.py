from m5stack import lcd, axp
import json
import machine
import select
import time
import urequests
import wifiCfg

EXIT_PROCESSING = "exit processing"

def parse(packet):
    edh1 = packet[0:1]
    edh2 = packet[1:2]
    tid = packet[2:4]
    seoj = packet[4:7]
    deoj = packet[7:10]
    esv = packet[10:11]
    opc = packet[11:12]
    rest = packet[12:]
    props = []
    for _ in range(int.from_bytes(opc, "big")):
        epc = int.from_bytes(rest[0:1], "big")
        pdc = int.from_bytes(rest[1:2], "big")
        edt = rest[2:2+pdc]
        props.append((epc, edt))
        rest = rest[2+pdc:]
    return int.from_bytes(esv, "big"), props

def is_fail_esv(esv):
    return esv >> 4 == 5

class BP35A1:
    def __init__(self):
        self._uart = machine.UART(1, tx=0, rx=26)
        self._poll = select.poll()

    def _wait_readable(self, timeout=-1):
        if self._uart.any() > 0:
            return True
        return bool(self._poll.poll(timeout))

    def _process_response_with_status(self):
        response = b""
        while True:
            self._wait_readable()
            line = self._uart.readline().strip()
            if line.startswith(b"SK"):
                print("debug: " + str(line))
                pass
            elif line == b"OK":
                break
            elif b"FAIL" in line:
                print(line)
                break
            else:
                response = line

        return response

    def _process_response(self):
        while True:
            self._wait_readable()
            line = self._uart.readline().strip()
            if line.startswith(b"SK"):
                print("debug: " + str(line))
                continue
            return line

    def _process_response_with_value(self):
        while True:
            self._wait_readable()
            line = self._uart.readline().strip()
            if line.startswith(b"OK"):
                return line.split()[1]

    def _clear_read_buffer(self):
        if self._uart.any() > 0:
            self._uart.read()

    def _use_ascii_for_udp(self):
        self._uart.write(b"ROPT\r\n")
        if int(self._process_response_with_value(), 16) == 0:
            self._uart.write(b"WOPT 1\r\n")
            self._process_response_with_status()

    def _enable_echo_back(self):
        self._uart.write(b"SKSREG SFE 1\r\n")
        self._process_response_with_status()

    def _scan(self):
        duration = 6
        while True:
            self._uart.write(b"SKSCAN 2 FFFFFFFF {:x}\r\n".format(duration))
            self._process_response_with_status()
            pandesc = {}
            while True:
                self._wait_readable()
                line = self._uart.readline().strip()
                print("debug: " + str(line))
                if line.startswith(b"EVENT 22"):
                    # scan end event.
                    if pandesc:
                        return pandesc
                    print("found scan end event. retry...")
                    duration += 1
                    if duration > 9:
                        duration = 9
                    break
                if line.startswith(b"EPANDESC"):
                    for _ in range(6):
                        self._wait_readable()
                        line = self._uart.readline().strip()
                        k, v = line.split(b":")
                        pandesc[k] = v

                if (b"Channel" in pandesc and
                    b"Pan ID" in pandesc and
                    b"Addr" in pandesc):
                    print("pandesc: {}".format(pandesc))

    def init(self):
        self._uart.init(115200, bits=8, parity=None, stop=1, timeout=2000)
        self._poll.register(self._uart, select.POLLIN)

        self._clear_read_buffer()
        self._enable_echo_back()
        self._use_ascii_for_udp()

    def connect(self, id_, password):
        self._uart.write(b"SKSETPWD C {:s}\r\n".format(password))
        self._process_response_with_status()

        self._uart.write(b"SKSETRBID {:s}\r\n".format(id_))
        self._process_response_with_status()

        while True:
            pandesc = self._scan()

            self._uart.write(b"SKSREG S2 {:s}\r\n".format(pandesc[b"Channel"]))
            self._process_response_with_status()
            self._uart.write(b"SKSREG S3 {:s}\r\n".format(pandesc[b"Pan ID"]))
            self._process_response_with_status()
            self._uart.write(b"SKLL64 {:s}\r\n".format(pandesc[b"Addr"]))
            self._ipv6addr = self._process_response()
            self._uart.write(b"SKJOIN {:s}\r\n".format(self._ipv6addr))
            self._process_response_with_status()
            while True:
                self._wait_readable()
                line = self._uart.readline().strip()
                print("debug: " + str(line))
                if line.startswith(b"EVENT 24"):
                    print("auth failed. retry...")
                    time.sleep(5)
                    break
                elif line.startswith(b"EVENT 25"):
                    print("auth succeeded")
                    return

    def process_events(self, handler, timeout=-1):
        events = 0
        start_time = time.time()

        while True:
            if timeout < 0:
                self._wait_readable()
            else:
                diff = (time.time() - start_time) * 1000
                if diff > timeout:
                    return events
                if not self._wait_readable(timeout - diff):
                    return events

            line = self._uart.readline().strip()
            if line.startswith(b"ERXUDP"):
                events += 1
                print("debug: " + str(line))
                sep = line.split()
                sport = int(sep[4], 16)
                if sport != 3610:
                    continue
                hex_data = sep[-1]
                data = b""
                for i in range(0, len(hex_data), 2):
                    data += int(hex_data[i:i+2], 16).to_bytes(1, "big")
                esv, props = parse(data)
                if handler(esv, props) == EXIT_PROCESSING:
                    return events

    def send_get_epc_value_command(self, epc):
        command = b"\x10\x81\x00\x01\x05\xFF\x01\x02\x88\x01\x62\x01" + epc.to_bytes(1, "big") + b"\x00"
        self._uart.write(b"SKSENDTO 1 {:s} 0E1A 1 {:04x} ".format(self._ipv6addr, len(command)))
        self._uart.write(command)
        self._process_response_with_status()

    def get_epc_value(self, target_epc):
        value = None
        def handler(esv, props):
            nonlocal value
            for (epc, edt) in props:
                if epc == target_epc:
                    if not is_fail_esv(esv):
                        value = edt
                    return EXIT_PROCESSING

        self.send_get_epc_value_command(target_epc)
        self.process_events(handler, 20000)
        return value

    def get_coefficient(self):
        value = self.get_epc_value(0xd3)
        if value is None:
            return 1

        return int.from_bytes(value, "big")

    def get_unit_for_cumulate(self):
        value = self.get_epc_value(0xe1)
        if value is None:
            return 1

        value = int.from_bytes(value, "big")
        if 0 <= value <= 4:
            return 0.1 ** value
        if 0xa <= value <= 0xd:
            return 10 ** (value - 0xa + 1)
        return 1

class Viewer:
    def __init__(self):
        self._using_watt = None
        self._cumulative_kwatt = None
        self._error_count = 0
        self._start = time.time()
        self._print_y = 0

    def set_using_watt(self, value):
        self._using_watt = value

    def set_cumulative_kwatt(self, value):
        self._cumulative_kwatt = value

    def inc_error_count(self):
        self._error_count += 1

    def _lcd_clear(self):
        lcd.clear()
        self._print_y = 0

    def _lcd_println(self, text):
        h = lcd.fontSize()[1]
        lcd.print(text, 0, (h + 1) * self._print_y, lcd.WHITE)
        self._print_y += 1

    def show(self):
        self._lcd_clear()

        if self._using_watt is None:
            self._lcd_println("n/a")
        else:
            self._lcd_println(str(self._using_watt) + "W")
        if self._cumulative_kwatt is None:
            self._lcd_println("n/a")
        else:
            self._lcd_println(str(self._cumulative_kwatt) + "kWh")

        total_sec = time.time() - self._start
        d = total_sec // 86400
        h = total_sec // 3600 % 24
        m = total_sec // 60 % 60
        s = total_sec % 60
        self._lcd_println("{}d{:02d}:{:02d}:{:02d}".format(d, h, m, s))
        self._lcd_println(str(axp.getBatVoltage()) + "V")
        self._lcd_println("error " + str(self._error_count))

class Reporter:
    def __init__(self, url):
        self._url = url

    def _report(self, name, value):
        try:
            res = urequests.post(
                url=self._url + "/metrics/job/pushgateway",
                data="{} {}\n".format(name, value)
            )
            try:
                if res.status_code == 200:
                    print("report succeeded. name={}, value={}".format(name, value))
                    return True
                print("report error: " + res.text)
                return False
            finally:
                res.close()
        except Exception as e:
            print("report error: " + str(e))
            return False

    def report_using_watt(self, value):
        return self._report("using_watt", value)

    def report_cumulative_kwatt(self, value):
        return self._report("used_watt_total", value * 1000)

def main():
    with open("/flash/watt_reader.json") as f:
        config = json.load(f)

    wifiCfg.autoConnect(lcdShow=True)

    viewer = Viewer()
    reporter = Reporter(config["pushgateway_url"])

    bp35a1 = BP35A1()
    bp35a1.init()
    print("bp35a1 initiated")
    bp35a1.connect(
        config["id"],
        config["password"],
    )
    print("smart meter connected")
    coefficient = bp35a1.get_coefficient()
    print("coefficient: " + str(coefficient))
    unit_for_cumulate = bp35a1.get_unit_for_cumulate()
    print("unit: " + str(unit_for_cumulate))

    def handler(esv, props):
        if is_fail_esv(esv):
            return

        for (epc, edt) in props:
            if epc == 0xe7:
                watt = int.from_bytes(edt, "big")
                viewer.set_using_watt(watt)
                if not reporter.report_using_watt(watt):
                    viewer.inc_error_count()
            if epc == 0xea:
                ckwatt = int.from_bytes(edt[7:11], "big") * coefficient * unit_for_cumulate
                viewer.set_cumulative_kwatt(ckwatt)
                if not reporter.report_cumulative_kwatt(ckwatt):
                    viewer.inc_error_count()

        viewer.show()

    timeout_count = 0

    while True:
        bp35a1.send_get_epc_value_command(0xe7)
        if bp35a1.process_events(handler, 20000) == 0:
            timeout_count += 1
        else:
            timeout_count = 0
        if timeout_count > 5:
            print("smart meter reconnecting...")
            bp35a1.connect(
                config["id"],
                config["password"],
            )
            print("smart meter connected")
            timeout_count = 0

main()
