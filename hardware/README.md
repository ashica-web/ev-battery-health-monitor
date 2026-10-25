# Hardware

## prototype/

Breadboard build on a single ESP32. Pin map is in the main [README](../README.md#prototype-hardware) and at the top of `firmware/EV_SINGLE_ESP32/EV_SINGLE_ESP32.ino`.

Put the wiring diagram and photos of the rig here.

Wiring reminders:
- DS18B20 and DHT11 on **3.3 V**, with their pull-ups also to 3.3 V. At 5 V the data line over-volts the ESP32 pin.
- ADS1115 powered from 5 V (gain ±4.096 V), ADDR to GND (address 0x48).
- All analog inputs on ADC1 pins (32–39).
- Flyback diode across the fan; fan and load on their own supply, grounds common.

## pcb/

KiCad project for the product board (rev A.1): BQ76952 battery monitor (3–16S), LM5164 buck, ESP32-S3, SHT40, SGP41, mic and swelling sensor headers, 4 layers.

Put the `.kicad_pro`, `.kicad_sch`, `.kicad_pcb` files and the BOM here. Gerbers can go in `pcb/gerbers/`.

Part-by-part rationale: [`../docs/bms-revA1-why-each-part.md`](../docs/bms-revA1-why-each-part.md).
