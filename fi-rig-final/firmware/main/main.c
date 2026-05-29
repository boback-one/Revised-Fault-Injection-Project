/*
 * ESP32-S3 Fault Injection Rig — Glitch Engine Firmware
 * Target: ESP-IDF v5.x, ESP32-S3
 *
 * Peripherals used:
 *   RMT     — glitch pulse generation (12.5ns resolution @ 80MHz)
 *   GPTimer — trigger delay (hardware, CPU-bypass)
 *   PCNT    — clock signal observation (edge counting/anomaly detect)
 *   ADC1+DMA— power rail voltage sampling
 *   I2S/LCD — parallel memory bus capture (8-bit @ 40MHz)
 *   UART0   — host command interface (USB-CDC)
 *
 * Glitch delivery path:
 *   GPIO → TC4427A gate driver → AO3400 N-ch MOSFET → Target VCC (crowbar)
 *
 * Rail selection:
 *   GPIO[3] → 74HC4051 8:1 mux → 8 injection points
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "driver/rmt_tx.h"
#include "driver/gptimer.h"
#include "driver/pulse_cnt.h"
#include "driver/adc.h"
#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp_adc/adc_continuous.h"
#include "esp_log.h"
#include "esp_rom_sys.h"

static const char *TAG = "GLITCH";

/* ───────────────────────────── PIN MAP ──────────────────────────────────── */

#define PIN_GLITCH_OUT      GPIO_NUM_4   // → TC4427A IN → AO3400 GATE
#define PIN_TRIGGER_IN      GPIO_NUM_5   // External trigger input (comparator out)
#define PIN_TARGET_RESET    GPIO_NUM_6   // Target /RESET line (active low)
#define PIN_TARGET_UART_RX  GPIO_NUM_7   // Target UART RX (response monitor)
#define PIN_MUX_A           GPIO_NUM_8   // 74HC4051 channel select A
#define PIN_MUX_B           GPIO_NUM_9   //                           B
#define PIN_MUX_C           GPIO_NUM_10  //                           C
#define PIN_CLOCK_OBS       GPIO_NUM_11  // Target clock → 74HC14 → here (PCNT)
#define PIN_ADC_POWER       ADC1_CHANNEL_0  // GPIO1 — power rail (via INA333)
#define PIN_BUS_D0          GPIO_NUM_15  // Memory bus D0–D7 (74LVC245 buffered)
// D1=16, D2=17, D3=18, D4=19, D5=20, D6=21, D7=22

/* ──────────────────────────── CONSTANTS ────────────────────────────────── */

#define RMT_RESOLUTION_HZ   80000000UL  // 80 MHz → 12.5ns/tick
#define GLITCH_MIN_NS       10
#define GLITCH_MAX_NS       50000
#define DELAY_MAX_NS        1000000UL   // 1ms max trigger delay
#define MAX_RAILS           8
#define UART_BUF_SIZE       512
#define ADC_SAMPLE_COUNT    256         // samples per glitch capture window
#define CMD_MAX_LEN         128

/* ──────────────────────────── DATA TYPES ───────────────────────────────── */

typedef enum {
    RESULT_OK       = 0,  // Target normal
    RESULT_FAULT    = 1,  // Anomaly detected (corrupted response)
    RESULT_CRASH    = 2,  // Target unresponsive
    RESULT_TIMEOUT  = 3,  // No response within window
} glitch_result_t;

typedef struct {
    uint32_t trigger_delay_ns;   // delay from trigger input to glitch start
    uint32_t glitch_width_ns;    // glitch pulse duration
    uint8_t  target_rail;        // 0–7, maps to 74HC4051 channel
    uint8_t  repeat;             // number of attempts at this parameter set
    uint32_t capture_window_ns;  // ADC capture window after glitch
    uint8_t  expect_byte;        // expected response byte from target (0xFF = any)
} glitch_params_t;

typedef struct {
    glitch_params_t  params;
    glitch_result_t  result;
    uint32_t         adc_min_mv;    // min rail voltage during glitch
    uint32_t         adc_max_mv;    // max rail voltage during glitch
    uint32_t         clock_edges;   // edges counted during capture window
    uint8_t          response_byte; // actual byte received from target
    uint64_t         timestamp_us;  // esp_timer_get_time()
} glitch_record_t;

/* ──────────────────────────── GLOBALS ──────────────────────────────────── */

static rmt_channel_handle_t  s_rmt_chan     = NULL;
static rmt_encoder_handle_t  s_rmt_encoder  = NULL;
static gptimer_handle_t      s_delay_timer  = NULL;
static pcnt_unit_handle_t    s_pcnt_unit    = NULL;
static adc_continuous_handle_t s_adc_handle = NULL;
static QueueHandle_t         s_result_queue = NULL;
static volatile bool         s_glitch_fired = false;
static volatile bool         s_trigger_armed = false;

/* ──────────────────────────── RMT ENCODER ──────────────────────────────── */

// Simple copy encoder — we hand RMT a pre-built symbol directly
typedef struct {
    rmt_encoder_t base;
    rmt_encoder_t *copy_encoder;
} glitch_encoder_t;

static size_t IRAM_ATTR glitch_encode(
    rmt_encoder_t *encoder,
    rmt_channel_handle_t channel,
    const void *primary_data,
    size_t data_size,
    rmt_encode_state_t *ret_state)
{
    glitch_encoder_t *enc = __containerof(encoder, glitch_encoder_t, base);
    return enc->copy_encoder->encode(enc->copy_encoder, channel,
                                     primary_data, data_size, ret_state);
}

static esp_err_t glitch_encoder_reset(rmt_encoder_t *encoder) {
    glitch_encoder_t *enc = __containerof(encoder, glitch_encoder_t, base);
    return enc->copy_encoder->reset(enc->copy_encoder);
}

static esp_err_t glitch_encoder_del(rmt_encoder_t *encoder) {
    glitch_encoder_t *enc = __containerof(encoder, glitch_encoder_t, base);
    enc->copy_encoder->del(enc->copy_encoder);
    free(enc);
    return ESP_OK;
}

static esp_err_t create_glitch_encoder(rmt_encoder_handle_t *ret_encoder) {
    glitch_encoder_t *enc = calloc(1, sizeof(glitch_encoder_t));
    if (!enc) return ESP_ERR_NO_MEM;

    enc->base.encode   = glitch_encode;
    enc->base.reset    = glitch_encoder_reset;
    enc->base.del      = glitch_encoder_del;

    rmt_copy_encoder_config_t copy_cfg = {};
    ESP_ERROR_CHECK(rmt_new_copy_encoder(&copy_cfg, &enc->copy_encoder));

    *ret_encoder = &enc->base;
    return ESP_OK;
}

/* ──────────────────────────── HARDWARE INIT ────────────────────────────── */

static void init_rmt(void) {
    rmt_tx_channel_config_t ch_cfg = {
        .gpio_num          = PIN_GLITCH_OUT,
        .clk_src           = RMT_CLK_SRC_DEFAULT,  // 80 MHz
        .resolution_hz     = RMT_RESOLUTION_HZ,
        .mem_block_symbols = 64,
        .trans_queue_depth = 4,
        .flags.invert_out  = false,
        .flags.with_dma    = false,
    };
    ESP_ERROR_CHECK(rmt_new_tx_channel(&ch_cfg, &s_rmt_chan));
    ESP_ERROR_CHECK(create_glitch_encoder(&s_rmt_encoder));
    ESP_ERROR_CHECK(rmt_enable(s_rmt_chan));
    ESP_LOGI(TAG, "RMT init OK — %.1f ns resolution",
             1e9 / RMT_RESOLUTION_HZ);
}

static void init_mux_gpios(void) {
    gpio_config_t io = {
        .pin_bit_mask = (1ULL << PIN_MUX_A) |
                        (1ULL << PIN_MUX_B) |
                        (1ULL << PIN_MUX_C) |
                        (1ULL << PIN_TARGET_RESET),
        .mode         = GPIO_MODE_OUTPUT,
        .pull_up_en   = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&io));
    gpio_set_level(PIN_TARGET_RESET, 1); // deassert reset
}

static void init_pcnt(void) {
    pcnt_unit_config_t unit_cfg = {
        .low_limit  = -32768,
        .high_limit = 32767,
    };
    ESP_ERROR_CHECK(pcnt_new_unit(&unit_cfg, &s_pcnt_unit));

    pcnt_chan_config_t chan_cfg = {
        .edge_gpio_num  = PIN_CLOCK_OBS,
        .level_gpio_num = -1,
    };
    pcnt_channel_handle_t pcnt_chan;
    ESP_ERROR_CHECK(pcnt_new_channel(s_pcnt_unit, &chan_cfg, &pcnt_chan));
    ESP_ERROR_CHECK(pcnt_channel_set_edge_action(pcnt_chan,
                    PCNT_CHANNEL_EDGE_ACTION_INCREASE,
                    PCNT_CHANNEL_EDGE_ACTION_HOLD));
    ESP_ERROR_CHECK(pcnt_unit_enable(s_pcnt_unit));
    ESP_ERROR_CHECK(pcnt_unit_clear_count(s_pcnt_unit));
    ESP_ERROR_CHECK(pcnt_unit_start(s_pcnt_unit));
    ESP_LOGI(TAG, "PCNT (clock observer) init OK");
}

static void init_adc(void) {
    adc_continuous_handle_cfg_t adc_cfg = {
        .max_store_buf_size = 4096,
        .conv_frame_size    = ADC_SAMPLE_COUNT * SOC_ADC_DIGI_DATA_BYTES_PER_CONV,
    };
    ESP_ERROR_CHECK(adc_continuous_new_handle(&adc_cfg, &s_adc_handle));

    adc_continuous_config_t dig_cfg = {
        .sample_freq_hz  = 2000000,  // 2 MSPS
        .conv_mode       = ADC_CONV_SINGLE_UNIT_1,
        .format          = ADC_DIGI_OUTPUT_FORMAT_TYPE2,
    };
    adc_digi_pattern_config_t pattern = {
        .atten     = ADC_ATTEN_DB_11,
        .channel   = PIN_ADC_POWER,
        .unit      = ADC_UNIT_1,
        .bit_width = SOC_ADC_DIGI_MAX_BITWIDTH,
    };
    dig_cfg.pattern_num = 1;
    dig_cfg.adc_pattern = &pattern;
    ESP_ERROR_CHECK(adc_continuous_config(s_adc_handle, &dig_cfg));
    ESP_LOGI(TAG, "ADC continuous init OK — 2 MSPS");
}

/* ──────────────────────────── RAIL MUX ─────────────────────────────────── */

static void select_rail(uint8_t rail) {
    gpio_set_level(PIN_MUX_A, rail & 0x01);
    gpio_set_level(PIN_MUX_B, (rail >> 1) & 0x01);
    gpio_set_level(PIN_MUX_C, (rail >> 2) & 0x01);
    esp_rom_delay_us(1); // settle time for 74HC4051
}

/* ──────────────────────────── GLITCH FIRE ──────────────────────────────── */

/*
 * Build an RMT symbol for the glitch pulse.
 *  level0=1 (MOSFET ON = crowbar active) for glitch_width ticks
 *  level1=0 (MOSFET OFF) for 1 tick (minimum idle)
 */
static rmt_symbol_word_t build_glitch_symbol(uint32_t width_ns) {
    uint32_t ticks = (uint32_t)((uint64_t)width_ns * RMT_RESOLUTION_HZ / 1000000000ULL);
    if (ticks < 1)  ticks = 1;
    if (ticks > 0x7FFF) ticks = 0x7FFF;  // RMT duration field is 15 bits

    rmt_symbol_word_t sym = {
        .level0    = 1,
        .duration0 = ticks,
        .level1    = 0,
        .duration1 = 1,
    };
    return sym;
}

static esp_err_t fire_glitch(const glitch_params_t *p) {
    rmt_symbol_word_t sym = build_glitch_symbol(p->glitch_width_ns);
    rmt_transmit_config_t tx_cfg = {
        .loop_count = 0,  // single shot
        .flags.eot_level = 0,  // return to 0 after
    };
    esp_err_t ret = rmt_transmit(s_rmt_chan, s_rmt_encoder,
                                 &sym, sizeof(sym), &tx_cfg);
    if (ret == ESP_OK) {
        s_glitch_fired = true;
    }
    return ret;
}

/* ──────────────────────────── TIMER CALLBACK ───────────────────────────── */

// Called from hardware timer ISR after trigger_delay expires
static bool IRAM_ATTR on_timer_alarm(gptimer_handle_t timer,
                                     const gptimer_alarm_event_data_t *edata,
                                     void *user_ctx) {
    const glitch_params_t *p = (const glitch_params_t *)user_ctx;

    // Select rail (already done before arming, but ensure)
    // Fire glitch — RMT is DMA-driven, call is ISR-safe
    rmt_symbol_word_t sym = build_glitch_symbol(p->glitch_width_ns);
    rmt_transmit_config_t tx_cfg = { .loop_count = 0, .flags.eot_level = 0 };
    rmt_transmit(s_rmt_chan, s_rmt_encoder, &sym, sizeof(sym), &tx_cfg);
    s_glitch_fired = true;

    return false; // no yield needed
}

static void init_delay_timer(void) {
    gptimer_config_t timer_cfg = {
        .clk_src       = GPTIMER_CLK_SRC_DEFAULT,
        .direction     = GPTIMER_COUNT_UP,
        .resolution_hz = 1000000000UL,  // 1 GHz → 1ns/tick (limited by HW to ~10ns)
    };
    // Note: actual ESP32-S3 timer resolution is ~10ns; 1GHz is requested,
    // IDF will clamp to max HW capability.
    ESP_ERROR_CHECK(gptimer_new_timer(&timer_cfg, &s_delay_timer));

    gptimer_event_callbacks_t cbs = { .on_alarm = on_timer_alarm };
    // user_ctx set per-glitch in arm function
    ESP_ERROR_CHECK(gptimer_register_event_callbacks(s_delay_timer, &cbs, NULL));
    ESP_ERROR_CHECK(gptimer_enable(s_delay_timer));
    ESP_LOGI(TAG, "GPTimer init OK");
}

/* ──────────────────────────── GLITCH ORCHESTRATOR ──────────────────────── */

static void target_reset(uint32_t hold_us) {
    gpio_set_level(PIN_TARGET_RESET, 0);
    esp_rom_delay_us(hold_us);
    gpio_set_level(PIN_TARGET_RESET, 1);
}

static uint32_t adc_sample_rail_mv(void) {
    // Quick single-shot ADC read (blocking, ~2µs)
    // For continuous capture use adc_continuous_read() in a dedicated task
    int raw = 0;
    adc1_get_raw(PIN_ADC_POWER); // discard first (settling)
    raw = adc1_get_raw(PIN_ADC_POWER);
    // Convert: 12-bit, 3.9V range (11dB atten), scaled for INA333 gain
    // Adjust INA333_GAIN to match your Rg resistor
    #define INA333_GAIN     10.0f
    #define ADC_VREF_MV     3900.0f
    #define ADC_FULL_SCALE  4095.0f
    float mv = ((float)raw / ADC_FULL_SCALE) * ADC_VREF_MV / INA333_GAIN;
    return (uint32_t)mv;
}

glitch_result_t execute_glitch(const glitch_params_t *p,
                                glitch_record_t *out) {
    out->params    = *p;
    s_glitch_fired = false;

    // 1. Select target rail
    select_rail(p->target_rail);

    // 2. Reset and re-arm target
    target_reset(100);
    vTaskDelay(pdMS_TO_TICKS(1)); // let target boot

    // 3. Baseline ADC
    uint32_t baseline_mv = adc_sample_rail_mv();

    // 4. Clear clock counter
    pcnt_unit_clear_count(s_pcnt_unit);

    // 5. Arm timer for delayed glitch
    gptimer_alarm_config_t alarm = {
        .alarm_count            = p->trigger_delay_ns,
        .reload_count           = 0,
        .flags.auto_reload_on_alarm = false,
    };
    // Re-register callback with this params pointer as ctx
    gptimer_event_callbacks_t cbs = { .on_alarm = on_timer_alarm };
    gptimer_register_event_callbacks(s_delay_timer, &cbs, (void*)p);
    gptimer_set_alarm_action(s_delay_timer, &alarm);
    gptimer_set_raw_count(s_delay_timer, 0);
    gptimer_start(s_delay_timer);

    // 6. Wait for glitch + capture window
    uint32_t wait_us = (p->trigger_delay_ns + p->glitch_width_ns +
                        p->capture_window_ns) / 1000 + 500;
    esp_rom_delay_us(wait_us);
    gptimer_stop(s_delay_timer);

    // 7. Read observations
    int clock_count = 0;
    pcnt_unit_get_count(s_pcnt_unit, &clock_count);
    uint32_t post_mv = adc_sample_rail_mv();

    out->adc_min_mv   = (post_mv < baseline_mv) ? post_mv : baseline_mv;
    out->adc_max_mv   = baseline_mv;
    out->clock_edges  = (uint32_t)clock_count;
    out->timestamp_us = 0; // fill with esp_timer_get_time() in real build

    // 8. Check target response (poll UART from target)
    uint8_t rx_byte = 0;
    int bytes_read  = uart_read_bytes(UART_NUM_1, &rx_byte, 1,
                                      pdMS_TO_TICKS(10));

    if (!s_glitch_fired) {
        out->result = RESULT_TIMEOUT;
    } else if (bytes_read <= 0) {
        out->result = RESULT_CRASH;
    } else if (p->expect_byte != 0xFF && rx_byte != p->expect_byte) {
        out->result = RESULT_FAULT;  // ← interesting!
    } else {
        out->result = RESULT_OK;
    }

    out->response_byte = rx_byte;
    return out->result;
}

/* ──────────────────────────── HOST PROTOCOL ────────────────────────────── */

/*
 * Simple line-based ASCII protocol over USB-CDC (UART0):
 *
 *   GLITCH <delay_ns> <width_ns> <rail> <repeat> <window_ns> <expect_hex>
 *   RESET
 *   STATUS
 *   SWEEP <delay_start> <delay_end> <delay_step> <width_start> <width_end> <width_step> <rail>
 *
 * Response: JSON line per attempt
 *   {"d":<delay>,"w":<width>,"r":<rail>,"res":<0-3>,"mv":<min_mv>,"clk":<edges>,"byte":<hex>}
 */

static void send_result(const glitch_record_t *rec) {
    printf("{\"d\":%lu,\"w\":%lu,\"r\":%u,\"res\":%u,"
           "\"mv\":%lu,\"clk\":%lu,\"byte\":\"0x%02X\"}\n",
           rec->params.trigger_delay_ns,
           rec->params.glitch_width_ns,
           rec->params.target_rail,
           rec->result,
           rec->adc_min_mv,
           rec->clock_edges,
           rec->response_byte);
}

static void process_command(char *line) {
    char cmd[16] = {0};
    sscanf(line, "%15s", cmd);

    if (strcmp(cmd, "GLITCH") == 0) {
        glitch_params_t p = {0};
        uint32_t expect_hex = 0xFF;
        sscanf(line, "GLITCH %lu %lu %hhu %hhu %lu %lx",
               &p.trigger_delay_ns, &p.glitch_width_ns,
               &p.target_rail, &p.repeat,
               &p.capture_window_ns, &expect_hex);
        p.expect_byte = (uint8_t)expect_hex;
        if (p.repeat == 0) p.repeat = 1;

        for (int i = 0; i < p.repeat; i++) {
            glitch_record_t rec;
            execute_glitch(&p, &rec);
            send_result(&rec);
        }

    } else if (strcmp(cmd, "RESET") == 0) {
        target_reset(1000);
        printf("{\"status\":\"reset_ok\"}\n");

    } else if (strcmp(cmd, "STATUS") == 0) {
        printf("{\"status\":\"ready\",\"rmt_res_hz\":%lu,"
               "\"rails\":%d}\n",
               (uint32_t)RMT_RESOLUTION_HZ, MAX_RAILS);

    } else if (strcmp(cmd, "SWEEP") == 0) {
        uint32_t d_start, d_end, d_step, w_start, w_end, w_step;
        uint8_t  rail = 0;
        sscanf(line, "SWEEP %lu %lu %lu %lu %lu %lu %hhu",
               &d_start, &d_end, &d_step,
               &w_start, &w_end, &w_step, &rail);

        glitch_params_t p = {
            .target_rail       = rail,
            .repeat            = 1,
            .capture_window_ns = 10000,
            .expect_byte       = 0xFF,
        };

        for (uint32_t d = d_start; d <= d_end; d += d_step) {
            for (uint32_t w = w_start; w <= w_end; w += w_step) {
                p.trigger_delay_ns = d;
                p.glitch_width_ns  = w;
                glitch_record_t rec;
                execute_glitch(&p, &rec);
                send_result(&rec);
            }
        }
        printf("{\"status\":\"sweep_done\"}\n");

    } else {
        printf("{\"error\":\"unknown_cmd\",\"cmd\":\"%s\"}\n", cmd);
    }
}

/* ──────────────────────────── MAIN ─────────────────────────────────────── */

static void uart_task(void *arg) {
    uint8_t buf[CMD_MAX_LEN];
    int     pos = 0;

    while (1) {
        uint8_t c;
        int n = uart_read_bytes(UART_NUM_0, &c, 1, portMAX_DELAY);
        if (n <= 0) continue;

        if (c == '\n' || c == '\r') {
            if (pos > 0) {
                buf[pos] = '\0';
                process_command((char *)buf);
                pos = 0;
            }
        } else if (pos < CMD_MAX_LEN - 1) {
            buf[pos++] = c;
        }
    }
}

void app_main(void) {
    ESP_LOGI(TAG, "ESP32-S3 Fault Injection Rig booting...");

    // UART0 (host interface)
    uart_config_t uart_cfg = {
        .baud_rate  = 921600,
        .data_bits  = UART_DATA_8_BITS,
        .parity     = UART_PARITY_DISABLE,
        .stop_bits  = UART_STOP_BITS_1,
        .flow_ctrl  = UART_HW_FLOWCTRL_DISABLE,
    };
    ESP_ERROR_CHECK(uart_driver_install(UART_NUM_0, UART_BUF_SIZE, 0, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(UART_NUM_0, &uart_cfg));

    // UART1 (target monitor)
    ESP_ERROR_CHECK(uart_driver_install(UART_NUM_1, UART_BUF_SIZE, 0, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(UART_NUM_1, &uart_cfg));
    ESP_ERROR_CHECK(uart_set_pin(UART_NUM_1,
                                 UART_PIN_NO_CHANGE, PIN_TARGET_UART_RX,
                                 UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));

    init_mux_gpios();
    init_rmt();
    init_delay_timer();
    init_pcnt();
    init_adc();

    s_result_queue = xQueueCreate(32, sizeof(glitch_record_t));

    printf("{\"status\":\"boot_ok\",\"fw\":\"fi-rig-v1.0\"}\n");

    xTaskCreatePinnedToCore(uart_task, "uart_cmd", 4096, NULL, 10, NULL, 1);

    // Main task idles; work is driven by UART commands
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
