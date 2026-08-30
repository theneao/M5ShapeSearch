/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 *
 * 修改时间：2026-08-29
 * 修改作用：WiFi/服务器与市场数据拆分；Data 页接管 A/B 做左右选择，触屏/左滑负责确认与返回。
 * 使用方式：普通页 A 返回；Data 页 A/B 调值、点屏确认、左滑或屏幕 BACK 返回。
 *
 * 修改时间：2026-08-29
 * 修改作用：移除手工时间/日期菜单，增加非线性动画档位和服务器数据看板。
 * 使用方式：联网后自动校时；Settings -> Motion 切换流畅/省电，Dashboard 查看各周期数量。
 */
#include "app_setup.h"
#include <hal/hal.h>
#include <mooncake.h>
#include <mooncake_log.h>
#include <assets/assets.h>
#include <cmath>

using namespace mooncake;
using namespace view;
using namespace setup_workers;

AppSetup::AppSetup()
{
    setAppInfo().name = "Settings";
    setAppInfo().icon = (void*)&icon_setup;
}

void AppSetup::onCreate()
{
    mclog::tagInfo(getAppInfo().name, "on create");
    // open();
}

void AppSetup::onOpen()
{
    mclog::tagInfo(getAppInfo().name, "on open");

    _key_manager = std::make_unique<input::KeyManager>();

    // Reset state
    _destroy_menu    = false;
    _need_warm_reset = false;
    _magic_count     = 0;
    _touch_active    = false;

    _menu_sections = {
        {
            "Network & Data",
            {
                {"WiFi / Server",
                 [&]() {
                     _destroy_menu = true;
                     _worker       = std::make_unique<WifiSettingsWorker>();
                 }},
                {"Market Data",
                 [&]() {
                     _destroy_menu = true;
                     _worker       = std::make_unique<DataSettingsWorker>();
                 }},
                {"Server Dashboard",
                 [&]() {
                     _destroy_menu = true;
                     _worker       = std::make_unique<ServerDashboardWorker>();
                 }},
            },
        },
        {
            "Device",
            {
                {"Brightness",
                 [&]() {
                     _destroy_menu = true;
                     _worker       = std::make_unique<BrightnessWorker>();
                 }},
                {"Volume",
                 [&]() {
                     _destroy_menu = true;
                     _worker       = std::make_unique<VolumeWorker>();
                 }},
                {"Button",
                 [&]() {
                     _destroy_menu = true;
                     _worker       = std::make_unique<ButtonWorker>();
                 }},
                {"Motion",
                 [&]() {
                     _destroy_menu = true;
                     _worker       = std::make_unique<MotionWorker>();
                 }},
            },
        },
        {
            "Firmware",
            {
                {fmt::format("Version: {}", common::FirmwareVersion),
                 [&]() {
                     _magic_count++;
                     if (_magic_count >= 10) {
                         _magic_count  = 0;
                         _destroy_menu = true;
                         _worker       = std::make_unique<AboutWorker>();
                     }
                 }},
            },
        },
    };

    LvglLockGuard lock;

    _menu_page = std::make_unique<view::SelectMenuPage>(_menu_sections);
}

void AppSetup::onRunning()
{
    GetHAL().updateButtonStates();
    const input::KeyEvent key_event = _key_manager ? _key_manager->update(false) : input::KeyEvent::None;

    bool swipe_left = false;
    const Hal::TouchPoint touch = GetHAL().getTouchPoint();
    if (touch.num > 0) {
        if (!_touch_active) {
            _touch_active = true;
            _touch_start_x = touch.x;
            _touch_start_y = touch.y;
        }
        _touch_last_x = touch.x;
        _touch_last_y = touch.y;
    } else if (_touch_active) {
        const int dx = _touch_last_x - _touch_start_x;
        const int dy = _touch_last_y - _touch_start_y;
        swipe_left = dx <= -90 && std::abs(dy) <= 75;
        _touch_active = false;
    }

    if (key_event == input::KeyEvent::GoHome) {
        close();
        return;
    }

    LvglLockGuard lock;

    const bool key_consumed = _worker && _worker->handleKey(key_event);
    if ((!key_consumed && key_event == input::KeyEvent::GoPrevious) || swipe_left) {
        if (_worker) {
            _worker.reset();
            _menu_page = std::make_unique<view::SelectMenuPage>(_menu_sections);
        } else {
            close();
        }
        return;
    }

    if (_menu_page) {
        _menu_page->update();
    }

    if (_destroy_menu) {
        _menu_page.reset();
        _destroy_menu = false;
    }

    if (_worker) {
        _worker->update();
        if (_worker->isDone()) {
            _worker.reset();
            _menu_page = std::make_unique<view::SelectMenuPage>(_menu_sections);
        }
    }
}

void AppSetup::onClose()
{
    mclog::tagInfo(getAppInfo().name, "on close");

    _key_manager.reset();
    _touch_active = false;

    LvglLockGuard lock;

    _menu_sections.clear();
    _menu_page.reset();
    _worker.reset();

    if (_need_warm_reset) {
        // GetHAL().requestWarmReboot(6);
    }
}
