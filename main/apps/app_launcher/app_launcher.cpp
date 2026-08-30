/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 *
 * 修改时间：2026-08-29
 * 修改作用：首次引导页支持左侧 A 返回键退出，避免无触控/不知双键操作时卡住。
 *
 * 修改时间：2026-08-29
 * 修改作用：启动器创建即连接已保存 WiFi，联网后自动 SNTP 校时并写回 RTC。
 *
 * 修改时间：2026-08-29
 * 修改作用：WiFi 启动延后 3 秒并移出 LVGL 锁，避免开机阶段电流峰值和长临界区导致重启。
 */
#include "app_launcher.h"
#include <apps/common/status_bar/status_bar.h>
#include <hal/hal.h>
#include <mooncake.h>
#include <mooncake_log.h>
#include <cstdint>

using namespace mooncake;

void AppLauncher::onLauncherCreate()
{
    mclog::tagInfo(getAppInfo().name, "on create");

    open();
}

void AppLauncher::onLauncherOpen()
{
    mclog::tagInfo(getAppInfo().name, "on open");

    show_guide_page();

    {
        LvglLockGuard lock;
        create_launcher_view();
        if (_is_first_open) {
            _pending_status_bar_create = true;
            _status_bar_create_tick    = GetHAL().millis() + 800;
        } else {
            view::create_status_bar(0xEDF4FF, 0x385179, true);
        }
    }

    if (_should_play_boot_sfx) {
        _should_play_boot_sfx = false;
        GetHAL().playBootSfx();
    }

    _last_charge_check_tick = GetHAL().millis();
    _was_battery_charging   = GetHAL().isBatteryCharging();
    if (!_network_started) {
        _network_start_tick = GetHAL().millis() + 3000;
    }

    _is_first_open = false;
}

void AppLauncher::onLauncherRunning()
{
    uint32_t now = GetHAL().millis();
    if (!_network_started && now >= _network_start_tick) {
        // WiFi 初始化不应占用 LVGL 锁；连接和扫描本身仍是异步的。
        _network.initialize();
        _network_started = true;
    }

    LvglLockGuard lock;

    // Check pending status bar creation
    if (_pending_status_bar_create && !view::is_status_bar_created() && now >= _status_bar_create_tick) {
        view::create_status_bar(0xEDF4FF, 0x385179, false);
        _pending_status_bar_create = false;
    }

    // Pop status bar if battery start charging
    if (now - _last_charge_check_tick >= 1000) {
        _last_charge_check_tick = now;

        bool is_battery_charging = GetHAL().isBatteryCharging();
        if (is_battery_charging && !_was_battery_charging && view::is_status_bar_created() &&
            view::is_status_bar_hidden()) {
            view::show_status_bar();
        }

        _was_battery_charging = is_battery_charging;
    }

    _view->update();
    view::update_status_bar();
}

void AppLauncher::onLauncherClose()
{
    mclog::tagInfo(getAppInfo().name, "on close");

    LvglLockGuard lock;

    _pending_status_bar_create = false;
    _status_bar_create_tick    = 0;
    _last_charge_check_tick    = 0;
    _was_battery_charging      = false;
    _view.reset();
    view::destroy_status_bar();
}

void AppLauncher::onLauncherDestroy()
{
    mclog::tagInfo(getAppInfo().name, "on close");
}

void AppLauncher::create_launcher_view()
{
    _view = std::make_unique<view::LauncherView>();
    _view->init(getAppProps());
    _view->onAppClicked = [&](int appID) {
        mclog::tagInfo(getAppInfo().name, "handle open app, app id: {}", appID);
        openApp(appID);
    };
}

void AppLauncher::show_guide_page()
{
    if (!_is_first_open) {
        return;
    }

    if (!GetHAL().shouldShowGuide()) {
        return;
    }

    GetHAL().lvglLock();
    auto guide_page = std::make_unique<view::GuidePage>();
    GetHAL().lvglUnlock();

    _should_play_boot_sfx = false;
    GetHAL().playBootSfx();

    input::KeyManager key_manager;

    while (1) {
        GetHAL().feedTheDog();
        GetHAL().delay(50);

        key_manager.update();
        if (key_manager.getEvent() == input::KeyEvent::GoHome ||
            key_manager.getEvent() == input::KeyEvent::GoPrevious) {
            break;
        }
    }

    LvglLockGuard lock;
    guide_page.reset();
}
