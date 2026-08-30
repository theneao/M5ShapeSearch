/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 *
 * 修改时间：2026-08-29
 * 修改作用：启动器创建时立即联网，确保无需进入应用即可自动 SNTP 校时。
 *
 * 修改时间：2026-08-29
 * 修改作用：自动联网延后到首页稳定后，避免 WiFi 峰值负载与显示/音频启动阶段重叠。
 */
#pragma once
#include "view/view.h"
#include <apps/app_stock_selector/net_manager.h>
#include <mooncake.h>
#include <mooncake_templates.h>
#include <cstdint>
#include <memory>

class AppLauncher : public mooncake::templates::AppLauncherBase {
public:
    void onLauncherCreate() override;
    void onLauncherOpen() override;
    void onLauncherRunning() override;
    void onLauncherClose() override;
    void onLauncherDestroy() override;

private:
    stock_selector::NetManager _network;
    std::unique_ptr<view::LauncherView> _view;
    bool _is_first_open              = true;
    bool _pending_status_bar_create  = false;
    uint32_t _status_bar_create_tick = 0;
    uint32_t _last_charge_check_tick = 0;
    bool _was_battery_charging       = false;
    bool _should_play_boot_sfx       = true;
    bool _network_started            = false;
    uint32_t _network_start_tick     = 0;

    void create_launcher_view();
    void show_guide_page();
};
