/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 *
 * 修改时间：2026-08-29
 * 修改作用：整机仅安装 Shape Search 与 Settings 两个功能应用，移除其余演示页面。
 * 使用方式：启动器左右切换两个入口；所有子页使用 A 键返回，双键长按回启动器。
 *
 * 修改时间：2026-08-29
 * 修改作用：开机打印 reset reason，便于区分软件异常、看门狗、掉电和外部复位。
 *
 * 修改时间：2026-08-30
 * 修改作用：主业务循环每轮主动让出 2ms，避免空转循环持续争抢 LVGL 互斥锁和内存总线。
 * 使用方式：无需配置；LVGL 动画仍由独立的 CPU1 刷新任务驱动。
 */
#include <smooth_ui_toolkit.hpp>
#include <uitk/short_namespace.hpp>
#include <mooncake_log.h>
#include <mooncake.h>
#include <apps/apps.h>
#include <hal/hal.h>
#include <lv_demos.h>
#include <apps/common/audio/audio.h>
#include <esp_system.h>

using namespace mooncake;
using namespace smooth_ui_toolkit;

extern "C" void app_main(void)
{
    // Setup logger
    mclog::set_level(mclog::level_info);
    mclog::set_time_format(mclog::time_format_unix_milliseconds);
    mclog::tagInfo("BOOT", "reset reason: {}", static_cast<int>(esp_reset_reason()));

    // HAL init
    GetHAL().init();

    // Setup ui hal
    ui_hal::on_delay([](uint32_t ms) { GetHAL().delay(ms); });
    ui_hal::on_get_tick([]() { return GetHAL().millis(); });

    // Install apps
    GetMooncake().installApp(std::make_unique<AppLauncher>());
    GetMooncake().installApp(std::make_unique<AppStockSelector>());
    GetMooncake().installApp(std::make_unique<AppSetup>());

    // Main loop
    while (1) {
        GetHAL().feedTheDog();
        GetMooncake().update();
        // x-track 在每次 lv_task_handler 后进入 WFI。ESP32 上采用短暂阻塞达到同样目的：
        // 输入与业务仍有 500Hz 响应能力，同时让 UI、Wi-Fi 和 DMA 获得稳定调度窗口。
        vTaskDelay(pdMS_TO_TICKS(2));
    }
}
