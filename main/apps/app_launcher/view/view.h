/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 *
 * 修改时间：2026-08-29
 * 修改作用：首页按键切页改为可配置的非线性动画，Smooth 侧重丝滑，Eco 缩短动画。
 *
 * 修改时间：2026-08-30
 * 修改作用：移除超宽循环滚动页，改为单图标两段式短位移动画，限制每帧脏区并阻止动画重叠。
 * 使用方式：A/B 或左右滑切换；连续输入会合并为下一次切换。
 */
#pragma once
// 修改时间：2026-08-29
// 修改作用：改用 LVGL 原生滚动动画，避免每帧重复触发滚动事件和吸附计算。
#include <apps/common/arc_top_clock/arc_top_clock.h>
#include <apps/common/key_manager/key_manager.h>
#include <mooncake.h>
#include <smooth_ui_toolkit.hpp>
#include <uitk/short_namespace.hpp>
#include <smooth_lvgl.hpp>
#include <functional>
#include <vector>
#include <memory>

namespace view {

/**
 * @brief
 *
 */
class LauncherView {
public:
    ~LauncherView();

    enum State_t {
        STATE_STARTUP,
        STATE_NORMAL,
    };

    std::function<void(int appID)> onAppClicked;

    void init(std::vector<mooncake::AppProps_t> appPorps);
    void update();

private:
    std::unique_ptr<uitk::lvgl_cpp::Container> _panel;
    std::vector<std::unique_ptr<uitk::lvgl_cpp::Container>> _icon_panels;
    std::vector<std::unique_ptr<uitk::lvgl_cpp::Image>> _icon_images;
    std::vector<std::unique_ptr<uitk::lvgl_cpp::Container>> _lr_indicator_panels;
    std::vector<std::unique_ptr<uitk::lvgl_cpp::Image>> _lr_indicators_images;
    std::unique_ptr<view::ArcTopClock> _clock;
    std::unique_ptr<input::KeyManager> _key_manager;
    std::vector<mooncake::AppProps_t> _app_props;

    int _clicked_app_id = -1;
    int _current_index = 0;
    int _transition_direction = 0;
    int _queued_direction = 0;
    State_t _state      = STATE_STARTUP;
    bool _smooth_motion = true;
    bool _transition_running = false;

    static void icon_animation_set_x(void* object, int32_t value);
    static void icon_animation_phase_one_completed(lv_anim_t* animation);
    static void icon_animation_phase_two_completed(lv_anim_t* animation);
    static void gesture_event_cb(lv_event_t* event);
    void scroll_to_nearby_icon(int direction);
    void begin_icon_transition(int direction);
    void start_icon_enter_phase();
    void finish_icon_transition();
    void apply_current_icon();
    void handle_state_startup();
    void handle_state_normal();
};

/**
 * @brief
 *
 */
class GuidePage {
public:
    GuidePage();

private:
    std::unique_ptr<uitk::lvgl_cpp::Container> _panel;
    std::unique_ptr<uitk::lvgl_cpp::Image> _img;
};

}  // namespace view
