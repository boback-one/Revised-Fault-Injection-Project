I'm not responsible for any damage you may do or your actions.

Practical wiring specifics for the S3

VDD3P3 — this is your primary glitch rail. It feeds the CPU, SRAM, and peripherals. Remove the bulk decoupling caps near the chip (typically 10–100 µF) and replace with a small 100 nF bypass only — the large caps absorb your glitch
EN (pin 3) — active-low reset, pulled high through a resistor. Connect this to a GPIO on your FPGA so you can reset the chip programmatically between glitch attempts
GPIO0 (pin 27) — pulling low at boot forces the download/bootloader mode. Tie this to another FPGA GPIO for automated boot mode control
VDDA — the analog supply (also 3.3 V). Less common target, but worth monitoring
Clock: the S3 uses an internal PLL from a 40 MHz crystal. You can substitute the crystal with a clock output from your FPGA for full clock glitching control, though the internal PLL startup complicates this — easier to stick with voltage glitching


using an esp32 s3 for a fault-injection rig capable of delivering sub-microsecond voltage glitches to selected rails, 
while observing power, clock, and memory signals. 
This is a solid hardware security research project.

Building a sub-microsecond voltage fault injection rig using the ESP32-S3:

Architecture Overview
The ESP32-S3 serves as the orchestration core — it handles timing, triggering, data capture, and host communication. 
The actual glitch is delivered by dedicated analog/power hardware it controls.

1. Glitch Delivery Path
Choices: Crowbar (shunt) glitching — fast MOSFET pulls the rail low momentarily

VCC glitching on microcontrollers
Components: AO3400 or Si2302 (N-ch, <1ns rise time), series resistor to limit current

Series MOSFET glitching — cuts power briefly

Slower, less precise, but safer for target

Capacitive injection — couples a fast pulse onto the rail via small cap (10–100pF)

Non-invasive, good for EM fault injection adjacent

Recommended path for sub-µs:
ESP32-S3 GPIO → Level shifter (3.3V→5V) → Gate driver (TC4427/UCC27324)
                                                     ↓
                                            N-ch MOSFET (AO3400)
                                                     ↓
                                         Target VCC rail ← crowbar to GND
Timing hardware:
The ESP32-S3's RMT peripheral can generate pulses with ~12.5ns resolution (at 80 MHz). 
For sub-100ns glitches use the RMT + a fast gate driver combo — this gets you into the 20–50ns glitch width range.

2. ESP32-S3 Peripheral Assignment
FunctionPeripheralNotesGlitch trigger outputRMT12.5ns resolution,
DMA-driven, no CPU jitterClock signal observationPCNT (pulse counter)Up to 40 MHz inputPower rail ADC samplingADC1 + DMA~2 MSPS,
use ext. op-amp frontendMemory bus observationGPIO matrix + I2SParallel capture via I2S in camera modeTrigger input
(PC, UART)UART0 / USB-OTGHost control interfacePrecise delayGPTimer (hardware timer)~10ns jitter in ISR, ~1ns in RMT

3. Signal Observation Frontend
Power rail (VCC monitoring)
Target rail → voltage divider → INA333 (instrumentation amp) → ESP32-S3 ADC
Or better: use an external ADS8681 (16-bit, 1 MSPS, SPI) for cleaner captures — ESP32's internal ADC has ±30mV nonlinearity.
Clock observation

Feed target clock through a 74HC14 Schmitt trigger → GPIO with PCNT
PCNT can detect glitches (clock stretching, missing pulses, extra edges)

Memory/data bus

Use 74LVC245 bus buffers (level shift + buffering) → ESP32-S3 GPIOs
Capture with I2S in parallel mode (LCD_CAM peripheral): 8–16 bit wide, up to 40 MHz sample rate
This lets you snapshot data bus state around the glitch window


4. Trigger Synchronization
The hardest part of fault injection is latch-aligned triggering — hitting the right clock edge.
Strategies:

Opcode-triggered: Decode a known instruction sequence on the data bus → trigger glitch N cycles later
Power-side triggered: Detect a characteristic power spike (e.g., AES round) via comparator → delay → glitch
External trigger: Target emits a GPIO pulse at the start of a sensitive operation

Use the MCPWM peripheral's fault input or GPIO interrupt → RMT start for the fastest software trigger path (~200ns latency, deterministic).
For true hardware-latency triggering:
Comparator (LMV7235) output → ESP32-S3 RMT ext_start pin
This bypasses the CPU entirely — trigger latency drops to ~30ns.

5. Glitch Parameter Space
You'll need to sweep: ParameterRange to exploreControlWidth5ns – 5µsRMT duration registerOffset
(from trigger)0 – 100µsGPTimer delay before RMT startAmplitudeRail-dependentGate drive voltage / series RShapeSquare,
rampMultiple MOSFETs stagedRailVCC, I/O, PLLMultiplexer (74HC4051)

6. Rail Multiplexing
To target different rails without rewiring:
ESP32-S3 GPIO[3] → 74HC4051 (8:1 analog mux) → 8 rail injection points
The 74HC4051 has ~7Ω on-resistance and ~6ns switching time — fast enough for rail selection
(not for the glitch itself, which still goes through the dedicated MOSFET).

7. Firmware Structure
c// Core glitch engine (pseudocode)
typedef struct {
    uint32_t trigger_delay_ns;
    uint32_t glitch_width_ns;
    uint8_t  target_rail;
    uint8_t  repeat_count;
} glitch_params_t;

void glitch_arm(glitch_params_t *p) {
    // 1. Configure rail mux
    gpio_set_level(MUX_A, p->target_rail & 1);
    gpio_set_level(MUX_B, (p->target_rail >> 1) & 1);
    gpio_set_level(MUX_C, (p->target_rail >> 2) & 1);

    // 2. Pre-configure RMT symbol
    rmt_symbol_word_t glitch_sym = {
        .level0 = 1, .duration0 = p->glitch_width_ns / 12,  // 80MHz ticks
        .level1 = 0, .duration1 = 1
    };
    rmt_transmit(rmt_chan, encoder, &glitch_sym, sizeof(glitch_sym), &tx_cfg);

    // 3. Start hardware timer for delay, which fires RMT
    gptimer_set_alarm_action(timer, &alarm_cfg);  // alarm triggers RMT ext_start
    gptimer_start(timer);
}
Sweep loop runs on the host (Python/Rust) over USB serial — the ESP32 just executes pre-parameterized glitch commands and streams back success/fault flags.

8. Response Detection
After each glitch attempt, determine outcome:

No effect: Target continues normally (read back known value)
Fault: Target returns corrupted data, skips instruction, unlocks feature
Crash: Target stops responding → host detects timeout → trigger reset via ESP32 GPIO → reset_n line

Detection methods:

UART response from target (parse for anomalies)
JTAG/SWD state read (if not locked) via ESP32-S3 bitbanging or dedicated FTDI
GPIO output observation (target signals "success" conditions)

End
