/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 *
 * 修改时间：2026-08-29
 * 修改作用：修复 LVGL 缓冲区字节数错误，取消多余的整屏帧缓冲中转，并提高流畅模式刷新率。
 * 使用方式：Settings -> Motion 选择 Smooth（约 60 FPS）或 Eco（约 30 FPS）。
 *
 * 修改时间：2026-08-30
 * 修改作用：AMOLED 延迟到 LVGL 首帧完整提交后再恢复亮度；绘制缓冲扩大到整屏，消除开机页分段闪烁。
 * 使用方式：无需设置，开机与首页重建时自动生效。
 *
 * 修改时间：2026-08-30
 * 修改作用：恢复 CO5300 专用 M5GFX 帧缓冲，修复直接写屏造成的黑屏和图片破损；LVGL 使用双 234 行缓冲。
 * 使用方式：无需设置；保留首帧熄屏提交与 Smooth/Eco 刷新档位。
 *
 * 修改时间：2026-08-30
 * 修改作用：关闭分块自动上屏，等待 LVGL 一帧最后一个 flush 后统一提交，消除滑动时上下画面割裂。
 * 使用方式：由 lv_display_flush_is_last() 自动判定帧边界，无需页面代码配合。
 *
 * 修改时间：2026-08-30
 * 修改作用：按 LVGL 官方建议把 PARTIAL 绘制缓冲缩至约 1/10 屏，并优先放入内部 DMA RAM；
 *           保留 AMOLED 帧缓冲的末次 flush 统一提交，减少 PSRAM 绘制延迟。
 * 使用方式：Smooth/Eco 设置不变；内部 RAM 不足时自动回退 PSRAM。
 */
#include "hal.h"
#include "utils/settings/settings.h"
#include <mooncake_log.h>
#include <M5GFX.h>
#include <lgfx/v1/panel/Panel_AMOLED.hpp>
#include <smooth_ui_toolkit.hpp>
#include <uitk/short_namespace.hpp>
#include <algorithm>
#include <memory>

static const std::string_view _tag = "HAL-Display";

/* -------------------------------------------------------------------------- */
/*                               Amoled display                               */
/* -------------------------------------------------------------------------- */
static constexpr gpio_num_t cfg_pin_sclk = GPIO_NUM_40;
static constexpr gpio_num_t cfg_pin_io0  = GPIO_NUM_41;
static constexpr gpio_num_t cfg_pin_io1  = GPIO_NUM_42;
static constexpr gpio_num_t cfg_pin_io2  = GPIO_NUM_46;
static constexpr gpio_num_t cfg_pin_io3  = GPIO_NUM_45;
static constexpr gpio_num_t cfg_pin_cs   = GPIO_NUM_39;
static constexpr gpio_num_t cfg_pin_te   = GPIO_NUM_38;
static constexpr gpio_num_t cfg_pin_rst  = GPIO_NUM_NC;

class Panel_CO5300 : public lgfx::Panel_AMOLED {
public:
    Panel_CO5300(void)
    {
        _cfg.memory_width = _cfg.panel_width = 480;
        _cfg.memory_height = _cfg.panel_height = 480;
        _write_depth                           = lgfx::color_depth_t::rgb565_2Byte;
        _read_depth                            = lgfx::color_depth_t::rgb565_2Byte;
    }

    const uint8_t *getInitCommands(uint8_t listno) const override
    {
        static constexpr uint8_t list0[] = {
            0x11, 0 + CMD_INIT_DELAY,
            150,  // Sleep out
            0xC4, 1,
            0x80, 0x35,
            1,    0x80,
            0x44, 2,
            0x01, 0xD2,  // Tear Effect Line = 0x1D2 == 466
            0x53, 1,
            0x20, 0x20,
            0,    0x36,
            1,    0,
            0x51, 1,
            0xA0, 0x29,
            0,    0xff,
            0xff  // end
        };
        switch (listno) {
            case 0:
                return list0;
            default:
                return nullptr;
        }
    }
};

class M5StopWatch : public M5GFX {
    lgfx::Bus_SPI _bus_instance;
    Panel_CO5300 _panel_instance;

public:
    M5StopWatch(void)
    {
    }

    // static constexpr int in_i2c_port                   = 0;  // I2C_NUM_0

    bool init_impl(bool use_reset, bool use_clear) override
    {
        {
            auto cfg = _bus_instance.config();

            cfg.freq_write = 80000000;
            cfg.freq_read  = 10000000;  // irrelevant

            cfg.pin_sclk = cfg_pin_sclk;
            cfg.pin_io0  = cfg_pin_io0;
            cfg.pin_io1  = cfg_pin_io1;
            cfg.pin_io2  = cfg_pin_io2;
            cfg.pin_io3  = cfg_pin_io3;

            cfg.spi_host    = SPI2_HOST;
            cfg.spi_mode    = 0;  // SPI_MODE0;
            cfg.spi_3wire   = true;
            cfg.dma_channel = SPI_DMA_CH_AUTO;

            _bus_instance.config(cfg);
            _panel_instance.setBus(&_bus_instance);
        }

        {
            auto cfg         = _panel_instance.config();
            cfg.pin_rst      = cfg_pin_rst;
            cfg.pin_cs       = cfg_pin_cs;
            cfg.panel_width  = 468;
            cfg.panel_height = 466;
            cfg.offset_x     = 6;
            cfg.offset_y     = 0;

            cfg.readable = false;

            _panel_instance.config(cfg);
        }

        setPanel(&_panel_instance);

        lgfx::pinMode(cfg_pin_te, lgfx::pin_mode_t::input_pullup);
        // lgfx::i2c::init(in_i2c_port);

        // io_expander.digitalWrite(PY32_L3B_EN_PIN, 1);
        // io_expander.digitalWrite(PY32_OLED_RST_PIN, 1);

        if (!LGFX_Device::init_impl(use_reset, use_clear)) return false;

        // CO5300 的 QSPI 连续写在较大 LVGL 区域上会出现图片破损。
        // 使用面板专用帧缓冲累积像素，再由其按偶数边界安全提交到 AMOLED。
        if (!enableFrameBuffer(false)) {
            mclog::tagError(_tag, "CO5300 framebuffer allocation failed");
            return false;
        }

        // LVGL 首帧准备好之前保持熄屏，避免面板初始化、清屏和分块绘制过程被用户看到。
        _panel_instance.setBrightness(0);

        return true;
    }

    bool enableFrameBuffer(bool auto_display = false)
    {
        if (_panel_instance.initPanelFb()) {
            auto fbPanel = _panel_instance.getPanelFb();
            if (fbPanel) {
                fbPanel->setBus(&_bus_instance);
                fbPanel->setAutoDisplay(auto_display);
                setPanel(fbPanel);
                return true;
            }
        }
        return false;
    }

    void disableFrameBuffer()
    {
        auto fbPanel = _panel_instance.getPanelFb();
        if (fbPanel) {
            _panel_instance.deinitPanelFb();
            setPanel(&_panel_instance);
        }
    }

    void setBrightness(uint8_t brightness)
    {
        _panel_instance.setBrightness(brightness);
    }
};

static std::unique_ptr<M5StopWatch> _display;
static std::unique_ptr<LGFX_Sprite> _canvas;

void Hal::display_init()
{
    mclog::tagInfo(_tag, "display init");

    _display = std::make_unique<M5StopWatch>();
    if (!_display->init()) {
        mclog::tagError(_tag, "display init failed");
        _display.reset();
    }

    // mclog::tagInfo(_tag, "create full screen canvas");
    // _canvas = std::make_unique<LGFX_Sprite>(_display.get());
    // _canvas->setPsram(true);
    // if (!_canvas->createSprite(_display->width(), _display->height())) {
    //     mclog::tagError(_tag, "canvas init failed");
    //     _canvas.reset();
    // }

    // 这里只读取目标亮度；lvgl_init() 完整提交启动页首帧后再点亮 AMOLED。
    getBackLightBrightness(true);
}

LGFX_Device &Hal::getDisplay()
{
    return *_display;
}

LGFX_Sprite &Hal::getCanvas()
{
    return *_canvas;
}

void Hal::updateCanvas()
{
    _canvas->pushSprite(0, 0);
}

void Hal::setBackLightBrightness(int brightness, bool saveToSettings)
{
    _bl_brightness = uitk::clamp(brightness, 0, 100);

    int set_target = uitk::map_range(_bl_brightness, 0, 100, 0, 255);
    _display->setBrightness(set_target);

    if (saveToSettings) {
        Settings settings(std::string(Hal::SettingsNs), true);
        settings.SetInt("bl_lev", _bl_brightness);
        mclog::tagInfo(_tag, "brightness saved to settings: {}", _bl_brightness);
    }
}

int Hal::getBackLightBrightness(bool loadFromSettings)
{
    if (loadFromSettings) {
        Settings settings(std::string(Hal::SettingsNs), false);
        _bl_brightness = settings.GetInt("bl_lev", 80);
        _bl_brightness = uitk::clamp(_bl_brightness, 10, 100);
        mclog::tagInfo(_tag, "brightness loaded from settings: {}", _bl_brightness);
    }
    return _bl_brightness;
}

/* -------------------------------------------------------------------------- */
/*                                  Touchpad                                  */
/* -------------------------------------------------------------------------- */
#include "drivers/cst820/cst820.h"

static std::unique_ptr<Cst820> _cst820;

void Hal::touchpad_init()
{
    mclog::tagInfo(_tag, "touchpad init");

    ioe_tp_reset();

    _cst820 = std::make_unique<Cst820>();
    if (!_cst820->begin(i2c_bus_get_internal_bus_handle(_i2c_bus))) {
        mclog::tagError(_tag, "touchpad init failed");
        _cst820.reset();
    }
}

Hal::TouchPoint Hal::getTouchPoint()
{
    Hal::TouchPoint point;
    if (_cst820 && _cst820->read()) {
        point.num = _cst820->getFingerNum();
        if (point.num > 0) {
            point.x = _cst820->getX();
            point.y = _cst820->getY();
        }
    }
    return point;
}

/* -------------------------------------------------------------------------- */
/*                                    Lvgl                                    */
/* -------------------------------------------------------------------------- */
// https://github.com/m5stack/lv_m5_emulator/blob/main/src/utility/lvgl_port_m5stack.cpp
#include <cstdlib>  // for aligned_alloc
#include <cstring>  // for memset
#include <lvgl.h>
#include <atomic>

static SemaphoreHandle_t xGuiSemaphore;
static std::atomic<bool> _lvgl_update_enabled = false;

// PARTIAL 模式按官方建议使用约 1/10 屏缓冲。48 为偶数，满足 CO5300 区域对齐要求。
static constexpr uint32_t kLvBufferLines        = 48;
static constexpr uint32_t kSmoothRefreshPeriod = 16;
static constexpr uint32_t kEcoRefreshPeriod    = 33;

static uint32_t lvgl_tick_get_cb()
{
    return static_cast<uint32_t>(esp_timer_get_time() / 1000ULL);
}

static void lvgl_rtos_task(void *pvParameter)
{
    (void)pvParameter;
    while (1) {
        uint32_t next_delay_ms = 10;
        if (_lvgl_update_enabled && pdTRUE == xSemaphoreTake(xGuiSemaphore, portMAX_DELAY)) {
            next_delay_ms = lv_timer_handler();
            xSemaphoreGive(xGuiSemaphore);
        }
        // 动画期间按 LVGL 给出的到期时间唤醒，避免固定 10ms 延时叠加到绘制耗时上。
        next_delay_ms = std::max<uint32_t>(1, std::min<uint32_t>(next_delay_ms, 10));
        vTaskDelay(pdMS_TO_TICKS(next_delay_ms));
    }
}

static void lvgl_rounder_event_cb(lv_event_t *event)
{
    lv_area_t *area = lv_event_get_invalidated_area(event);
    if (area == nullptr) {
        return;
    }

    // CO5300 要求起始 x 为偶数且宽度为偶数。
    area->x1 &= ~0x1;
    area->x2 |= 0x1;
}

static void lvgl_flush_cb(lv_display_t *disp, const lv_area_t *area, uint8_t *px_map)
{
    M5GFX &gfx = *(M5GFX *)lv_display_get_driver_data(disp);

    uint32_t w      = (area->x2 - area->x1 + 1);
    uint32_t h      = (area->y2 - area->y1 + 1);
    uint32_t pixels = w * h;

    gfx.startWrite();
    gfx.setAddrWindow(area->x1, area->y1, w, h);

    // Critical fix: Use safe pixel writing method to avoid M5GFX SIMD optimizations
    // Break large transfers into small chunks to avoid problematic copy_rgb_fast function
    const uint32_t SAFE_CHUNK_SIZE = 8192;  // 8K pixels per chunk, suitable for small buffer settings

    if (pixels > SAFE_CHUNK_SIZE) {
        // Chunked transmission for large data
        const lgfx::rgb565_t *src = (const lgfx::rgb565_t *)px_map;
        uint32_t remaining        = pixels;
        uint32_t offset           = 0;

        while (remaining > 0) {
            uint32_t chunk_size = (remaining > SAFE_CHUNK_SIZE) ? SAFE_CHUNK_SIZE : remaining;
            gfx.writePixels(src + offset, chunk_size);
            offset += chunk_size;
            remaining -= chunk_size;
        }
    } else {
        // Direct transmission for small data
        gfx.writePixels((lgfx::rgb565_t *)px_map, pixels);
    }

    gfx.endWrite();

    // LVGL 可能把同一帧拆成多个无效区域。前面的 flush 只更新内存帧缓冲，
    // 最后一个 flush 再一次性把累计区域提交到 CO5300，避免上下区域显示不同动画时刻。
    if (lv_display_flush_is_last(disp)) {
        gfx.display();
    }

    lv_display_flush_ready(disp);
}

static void lvgl_read_cb(lv_indev_t *indev, lv_indev_data_t *data)
{
    auto tp = GetHAL().getTouchPoint();
    if (tp.num == 0) {
        data->state = LV_INDEV_STATE_REL;
    } else {
        data->state   = LV_INDEV_STATE_PR;
        data->point.x = tp.x;
        data->point.y = tp.y;
    }
}

void Hal::lvgl_init()
{
    mclog::tagInfo(_tag, "lvgl init");

    lv_init();

    static lv_display_t *disp = lv_display_create(_display->width(), _display->height());
    if (disp == NULL) {
        printf("lv_display_create failed\n");
        return;
    }

    lv_display_set_driver_data(disp, _display.get());
    lv_display_set_flush_cb(disp, lvgl_flush_cb);
    lv_display_set_color_format(disp, LV_COLOR_FORMAT_RGB565);
    lv_display_add_event_cb(disp, lvgl_rounder_event_cb, LV_EVENT_INVALIDATE_AREA, nullptr);

    // 优先在片内 DMA RAM 绘制，避免 CPU 在 PSRAM 上进行圆角、透明度和图片混合。
    // AMOLED 自身帧缓冲继续负责把多个 partial flush 合并成一帧，最后一次 flush 才上屏。
    static const uint32_t buffer_size = _display->width() * kLvBufferLines *
                                        LV_COLOR_FORMAT_GET_SIZE(LV_COLOR_FORMAT_RGB565);
    auto allocate_draw_buffer = []() -> uint8_t* {
        uint8_t* buffer = static_cast<uint8_t*>(
            heap_caps_malloc(buffer_size, MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA | MALLOC_CAP_8BIT)
        );
        if (buffer == nullptr) {
            buffer = static_cast<uint8_t*>(
                heap_caps_malloc(buffer_size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)
            );
        }
        return buffer;
    };
    static uint8_t *buf1 = allocate_draw_buffer();
    static uint8_t *buf2 = allocate_draw_buffer();
    if (buf1 == nullptr) {
        mclog::tagError(_tag, "LVGL primary draw buffer allocation failed: {} bytes", buffer_size);
        return;
    }
    if (buf2 == nullptr) {
        mclog::tagWarn(_tag, "LVGL secondary draw buffer allocation failed, using single buffer");
    }
    lv_display_set_buffers(disp, buf1, buf2, buffer_size, LV_DISPLAY_RENDER_MODE_PARTIAL);

    Settings motion_settings("ui_motion", false);
    const bool smooth_motion = motion_settings.GetBool("smooth", true);
    lv_timer_set_period(lv_display_get_refr_timer(disp),
                        smooth_motion ? kSmoothRefreshPeriod : kEcoRefreshPeriod);
    mclog::tagInfo(_tag, "LVGL partial double buffer: {} lines, {} bytes, refresh={}ms", kLvBufferLines, buffer_size,
                   smooth_motion ? kSmoothRefreshPeriod : kEcoRefreshPeriod);

    lvTouchpad = lv_indev_create();
    LV_ASSERT_MALLOC(lvTouchpad);
    if (lvTouchpad == NULL) {
        printf("lv_indev_create failed\n");
        return;
    }
    lv_indev_set_driver_data(lvTouchpad, _display.get());
    lv_indev_set_type(lvTouchpad, LV_INDEV_TYPE_POINTER);
    lv_indev_set_read_cb(lvTouchpad, lvgl_read_cb);
    lv_indev_set_display(lvTouchpad, disp);

    xGuiSemaphore = xSemaphoreCreateMutex();
    lv_tick_set_cb(lvgl_tick_get_cb);

    // UI 任务尚未启动，此处可原子创建并完整提交首帧。提交完再恢复亮度，开机不会闪出中间态。
    uitk::lvgl_cpp::ScreenActive screen;
    screen.setBgColor(lv_color_black());
    GetHAL().bootLogo = std::make_unique<BootLogo>();
    lv_obj_invalidate(lv_screen_active());
    lv_refr_now(disp);
    setBackLightBrightness(getBackLightBrightness(false), false);

    // UI 固定在 CPU1，避开 CPU0 上的主循环和 HTTP 请求；较高优先级减少 WiFi 活动造成的帧间抖动。
    xTaskCreatePinnedToCore(lvgl_rtos_task, "lvgl_rtos_task", 4096 * 4, nullptr, 3, nullptr, 1);
    startLvglUpdate();
}

bool Hal::lvglLock()
{
    return xSemaphoreTake(xGuiSemaphore, portMAX_DELAY) == pdTRUE ? true : false;
}

void Hal::lvglUnlock()
{
    xSemaphoreGive(xGuiSemaphore);
}

void Hal::startLvglUpdate()
{
    _lvgl_update_enabled = true;
}

void Hal::stopLvglUpdate()
{
    _lvgl_update_enabled = false;
}

void Hal::setUiSmoothMode(bool smooth)
{
    lv_display_t *disp = lv_display_get_default();
    if (disp == nullptr) {
        return;
    }
    lv_timer_t *refresh_timer = lv_display_get_refr_timer(disp);
    if (refresh_timer != nullptr) {
        lv_timer_set_period(refresh_timer, smooth ? kSmoothRefreshPeriod : kEcoRefreshPeriod);
        lv_timer_ready(refresh_timer);
    }
}
