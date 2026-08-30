/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 *
 * 修改时间：2026-08-29
 * 修改作用：首页 A/B 切换使用 ease-out 非线性滚动，并提供 Smooth/Eco 两种动画时长。
 * 使用方式：Settings -> Motion 修改；下次回到首页时立即生效。
 */
#include "view.h"
// 修改时间：2026-08-29
// 修改作用：首页切换使用 LVGL 原生滚动执行器，并缓存标签透明度以减少无效重绘。
// 使用方式：按 A/B 或触屏左右滑动切换；Motion 设置会同时调整动画和全局刷新率。
// 修改时间：2026-08-30
// 修改作用：启动页销毁后立即以整屏缓冲提交稳定首页，避免首屏短暂残影或闪烁。
// 使用方式：开机或返回首页时自动生效。
// 修改时间：2026-08-30
// 修改作用：移除页面构造期间的同步强制刷新，避免与 LVGL 刷新任务竞争导致图层残缺。
// 修改时间：2026-08-30
// 修改作用：首页循环副本从 5 组减为 3 组，并在首帧前直接定位中间组，减少布局负担和启动跳帧。
// 修改时间：2026-08-30
// 修改作用：参考 x-track 的单属性根节点动画，移除滚动过程中逐帧标签透明度计算，缩短切换时间。
// 使用方式：A/B 或触屏滑动保持 ease-out；Smooth=240ms，Eco=100ms。
// 修改时间：2026-08-30
// 修改作用：针对 466×466 QSPI 圆屏，将超宽滚动容器重构为单图标短行程两段动画；
//           动画期间只刷新约 248×200 图标区域，并串行处理连续输入。
// 使用方式：A/B、左右箭头或左右滑动切换；点击中心图标进入当前功能。
#include <mooncake_log.h>
#include <assets/assets.h>
#include <functional>
#include <hal/hal.h>
#include <hal/utils/settings/settings.h>
#include <cstdint>
#include <vector>

using namespace view;
using namespace uitk;
using namespace uitk::lvgl_cpp;

/* -------------------------------------------------------------------------- */
/*                               Page indicator                               */
/* -------------------------------------------------------------------------- */
class PageIndicator {
public:
    const int dot_size     = 8;
    const int dot_size_big = 14;
    const int dot_gap      = 16;

    void init(int pageNum, int pageGap, lv_obj_t* parent, int posX, int posY)
    {
        _page_num = pageNum;
        _page_gap = pageGap;

        _panel = std::make_unique<Container>(parent);
        _panel->removeFlag(LV_OBJ_FLAG_SCROLLABLE);
        _panel->addFlag(LV_OBJ_FLAG_FLOATING);
        _panel->setAlign(LV_ALIGN_CENTER);
        _panel->setPadding(0, 0, 24, 24);
        _panel->setPos(posX, posY);
        _panel->setBorderWidth(0);
        _panel->setHeight(24);
        _panel->setWidth((pageNum * dot_size) + (pageNum - 1) * (dot_gap - dot_size) + 24 * 2);
        _panel->setBgOpa(0);

        for (int i = 0; i < pageNum; i++) {
            _dots.push_back(std::make_unique<Container>(_panel->get()));
            _dots.back()->setAlign(LV_ALIGN_CENTER);
            _dots.back()->setPos(i * dot_gap - (pageNum - 1) * dot_gap / 2, 0);
            _dots.back()->setBgColor(lv_color_hex(0xFFFFFF));
            _dots.back()->removeFlag(LV_OBJ_FLAG_SCROLLABLE);
            _dots.back()->setRadius(LV_RADIUS_CIRCLE);
            _dots.back()->setSize(dot_size, dot_size);
            _dots.back()->setBorderWidth(0);
        }

        jumpTo(0);
    }

    void jumpTo(int index)
    {
        if (index < 0 || index >= _page_num) {
            return;
        }
        _current_index = index;
        _last_index    = index;
        update_dots();
    }

    void update(int scrollValue)
    {
        _last_index = _current_index;

        // Calculate absolute index
        int abs_index = (scrollValue + _page_gap / 2) / _page_gap;

        // Map to 0 ~ N-1
        _current_index = abs_index % _page_num;
        if (_current_index < 0) {
            _current_index += _page_num;
        }

        if (_last_index != _current_index) {
            update_dots();
        }
    }

private:
    int _page_num = 0;
    int _page_gap = 0;

    int _current_index = 0;
    int _last_index    = 0;

    std::unique_ptr<Container> _panel;
    std::vector<std::unique_ptr<Container>> _dots;

    void update_dots()
    {
        for (int i = 0; i < _page_num; i++) {
            if (i == _current_index) {
                _dots[i]->setSize(dot_size_big, dot_size_big);
                _dots[i]->setOpa(255);
            } else {
                _dots[i]->setSize(dot_size, dot_size);
                _dots[i]->setOpa(128);
            }
        }
    }
};

/* -------------------------------------------------------------------------- */
/*                             Dynamic icon label                             */
/* -------------------------------------------------------------------------- */
class DynamicIconLabel {
public:
    const int pos_y           = 155;

    void init(const std::vector<std::string>& iconLabelTexts, int iconGap, lv_obj_t* parent)
    {
        _icon_label_texts = iconLabelTexts;
        _icon_gap         = iconGap;

        // Create floating label
        _label = std::make_unique<Label>(parent);
        _label->setTextColor(lv_color_hex(0xFFFFFF));
        _label->setTextFont(&MontserratSemiBold26);
        _label->setAlign(LV_ALIGN_CENTER);
        _label->addFlag(LV_OBJ_FLAG_FLOATING);
        _label->setOpa(255);

        jumpTo(0);
    }

    void jumpTo(int index)
    {
        if (index < 0 || index >= _icon_label_texts.size()) {
            return;
        }

        _current_index = index;
        _last_index    = index;

        // Update label
        _label->setText(_icon_label_texts[index]);
        _label->setPos(0, pos_y);
        set_opacity(255);
    }

    void update(int scrollValue)
    {
        _last_index = _current_index;

        // 只在最近图标改变时更新文本；不再把透明度作为第二条逐帧动画。
        _current_index        = (scrollValue + _icon_gap / 2) / _icon_gap;

        // Clamp index
        if (_current_index < 0) {
            _current_index = 0;
        }
        if (_current_index >= _icon_label_texts.size()) {
            _current_index = _icon_label_texts.size() - 1;
        }

        // If index changed, update label text
        if (_last_index != _current_index) {
            _label->setText(_icon_label_texts[_current_index]);
        }
    }

private:
    std::vector<std::string> _icon_label_texts;
    int _icon_gap      = 0;
    int _current_index = 0;
    int _last_index    = 0;
    int _last_opacity  = -1;

    std::unique_ptr<Label> _label;

    void set_opacity(lv_opa_t opacity)
    {
        if (_last_opacity == opacity) {
            return;
        }
        _last_opacity = opacity;
        _label->setOpa(opacity);
    }
};

static std::string _tag        = "LauncherView";
static constexpr int _icon_gap = 466;
static constexpr int _icon_transition_distance = 48;

static int _last_selected_index = 0;
static std::unique_ptr<PageIndicator> _page_indicator;
static std::unique_ptr<DynamicIconLabel> _dynamic_icon_label;

LauncherView::~LauncherView()
{
    _icon_images.clear();
    _icon_panels.clear();
    _lr_indicators_images.clear();
    _lr_indicator_panels.clear();
    _panel.reset();
    _page_indicator.reset();
    _dynamic_icon_label.reset();
}

void LauncherView::init(std::vector<mooncake::AppProps_t> appPorps)
{
    mclog::tagInfo(_tag, "init");

    _key_manager = std::make_unique<input::KeyManager>();
    _app_props = std::move(appPorps);
    Settings motion_settings("ui_motion", false);
    _smooth_motion = motion_settings.GetBool("smooth", true);

    /* ------------------------------ Screen setup ------------------------------ */
    ScreenActive screen;
    screen.removeFlag(LV_OBJ_FLAG_SCROLLABLE);

    /* ---------------------------------- Panel --------------------------------- */
    _panel = std::make_unique<Container>(lv_screen_active());
    _panel->setAlign(LV_ALIGN_CENTER);
    _panel->setSize(466, 466);
    _panel->setRadius(0);
    _panel->setBorderWidth(0);
    _panel->setBgColor(lv_color_hex(0x000000));
    _panel->removeFlag(LV_OBJ_FLAG_SCROLLABLE);
    _panel->setPaddingAll(0);
    lv_obj_add_event_cb(_panel->get(), &LauncherView::gesture_event_cb, LV_EVENT_GESTURE, this);

    /* ---------------------------------- Icons --------------------------------- */
    int icon_x = 0;
    int icon_y = -15;
    std::vector<std::string> icon_label_texts;
    std::vector<uint32_t> step_colors;

    for (const auto& props : _app_props) {
        icon_label_texts.push_back(props.info.name);
        uint32_t color = 0xDADADA;
        if (props.info.userData != nullptr) {
            color = *(uint32_t*)props.info.userData;
        }
        step_colors.push_back(color);
    }

    // 首页只保留一个中心图标对象。切换时替换图片源并做短行程位移，
    // 不再创建 3 组循环副本或移动 466px 宽的滚动容器。
    _icon_panels.push_back(std::make_unique<Container>(_panel->get()));
    _icon_panels.back()->setAlign(LV_ALIGN_CENTER);
    _icon_panels.back()->setSize(200, 200);
    _icon_panels.back()->setPos(icon_x, icon_y);
    _icon_panels.back()->setBorderWidth(0);
    _icon_panels.back()->removeFlag(LV_OBJ_FLAG_SCROLLABLE);
    _icon_panels.back()->setBgOpa(0);
    _icon_panels.back()->onClick().connect([this]() {
        if (!_app_props.empty() && !_transition_running) {
            _clicked_app_id = _app_props[_current_index].appID;
            _last_selected_index = _current_index;
        }
    });

    _icon_images.push_back(std::make_unique<Image>(_icon_panels.back()->get()));
    _icon_images.back()->setAlign(LV_ALIGN_CENTER);
    if (!_app_props.empty() && _app_props.front().info.icon != nullptr) {
        _icon_images.back()->setSrc(_app_props.front().info.icon);
    }

    /* ------------------------------ LR indicators ----------------------------- */
    // Go left indicator
    _lr_indicator_panels.push_back(std::make_unique<Container>(_panel->get()));
    _lr_indicator_panels.back()->setAlign(LV_ALIGN_CENTER);
    _lr_indicator_panels.back()->setSize(52, 160);
    _lr_indicator_panels.back()->setPos(-200, 0);
    _lr_indicator_panels.back()->setBorderWidth(0);
    _lr_indicator_panels.back()->addFlag(LV_OBJ_FLAG_FLOATING);
    _lr_indicator_panels.back()->removeFlag(LV_OBJ_FLAG_SCROLLABLE);
    _lr_indicator_panels.back()->setBgOpa(0);
    _lr_indicator_panels.back()->onClick().connect([this]() { scroll_to_nearby_icon(-1); });

    _lr_indicators_images.push_back(std::make_unique<Image>(_lr_indicator_panels.back()->get()));
    _lr_indicators_images.back()->setSrc(&icon_indicator_left);
    _lr_indicators_images.back()->align(LV_ALIGN_CENTER, 0, 0);

    // Go right indicator
    _lr_indicator_panels.push_back(std::make_unique<Container>(_panel->get()));
    _lr_indicator_panels.back()->setAlign(LV_ALIGN_CENTER);
    _lr_indicator_panels.back()->setSize(52, 160);
    _lr_indicator_panels.back()->setPos(200, 0);
    _lr_indicator_panels.back()->setBorderWidth(0);
    _lr_indicator_panels.back()->addFlag(LV_OBJ_FLAG_FLOATING);
    _lr_indicator_panels.back()->removeFlag(LV_OBJ_FLAG_SCROLLABLE);
    _lr_indicator_panels.back()->setBgOpa(0);
    _lr_indicator_panels.back()->onClick().connect([this]() { scroll_to_nearby_icon(1); });

    _lr_indicators_images.push_back(std::make_unique<Image>(_lr_indicator_panels.back()->get()));
    _lr_indicators_images.back()->setSrc(&icon_indicator_right);
    _lr_indicators_images.back()->align(LV_ALIGN_CENTER, 0, 0);

    /* ------------------------------ Page indicator ---------------------------- */
    _page_indicator = std::make_unique<PageIndicator>();
    _page_indicator->init(_app_props.size(), _icon_gap, _panel->get(), 0, 200);

    /* --------------------------- Dynamic icon label --------------------------- */
    _dynamic_icon_label = std::make_unique<DynamicIconLabel>();
    _dynamic_icon_label->init(icon_label_texts, _icon_gap, _panel->get());

    /* ---------------------------------- Clock --------------------------------- */
    _clock = std::make_unique<view::ArcTopClock>(_panel->get());
    _clock->init();
    _clock->align(LV_ALIGN_TOP_MID, 0, 4);
    _clock->addFlag(LV_OBJ_FLAG_FLOATING);

    /* ----------------------------- History restore ---------------------------- */
    if (!_app_props.empty()) {
        _current_index = uitk::clamp(_last_selected_index, 0, static_cast<int>(_app_props.size()) - 1);
    }
    apply_current_icon();

    _state = STATE_NORMAL;

    // Destory boot logo label
    GetHAL().bootLogo.reset();
    lv_obj_invalidate(lv_screen_active());
}

void LauncherView::update()
{
    if (_key_manager) {
        switch (_key_manager->update()) {
            case input::KeyEvent::GoPrevious:
                scroll_to_nearby_icon(-1);
                break;
            case input::KeyEvent::GoNext:
                scroll_to_nearby_icon(1);
                break;
            default:
                break;
        }
    }

    switch (_state) {
        case STATE_STARTUP:
            handle_state_startup();
            break;
        case STATE_NORMAL:
            handle_state_normal();
            break;
        default:
            break;
    }
}

void LauncherView::scroll_to_nearby_icon(int direction)
{
    begin_icon_transition(direction);
}

void LauncherView::icon_animation_set_x(void* object, int32_t value)
{
    lv_obj_set_x(static_cast<lv_obj_t*>(object), value);
}

void LauncherView::icon_animation_phase_one_completed(lv_anim_t* animation)
{
    auto* self = static_cast<LauncherView*>(lv_anim_get_user_data(animation));
    if (self != nullptr) {
        self->start_icon_enter_phase();
    }
}

void LauncherView::icon_animation_phase_two_completed(lv_anim_t* animation)
{
    auto* self = static_cast<LauncherView*>(lv_anim_get_user_data(animation));
    if (self != nullptr) {
        self->finish_icon_transition();
    }
}

void LauncherView::gesture_event_cb(lv_event_t* event)
{
    auto* self = static_cast<LauncherView*>(lv_event_get_user_data(event));
    lv_indev_t* indev = lv_indev_active();
    if (self == nullptr || indev == nullptr) {
        return;
    }
    const lv_dir_t direction = lv_indev_get_gesture_dir(indev);
    if (direction == LV_DIR_LEFT) {
        self->begin_icon_transition(1);
    } else if (direction == LV_DIR_RIGHT) {
        self->begin_icon_transition(-1);
    }
}

void LauncherView::begin_icon_transition(int direction)
{
    if (_app_props.size() < 2 || _icon_panels.empty() || direction == 0) {
        return;
    }
    direction = direction > 0 ? 1 : -1;
    if (_transition_running) {
        _queued_direction = direction;
        return;
    }

    _transition_running = true;
    _transition_direction = direction;
    lv_obj_t* icon = _icon_panels.front()->get();
    lv_anim_delete(icon, &LauncherView::icon_animation_set_x);

    lv_anim_t animation;
    lv_anim_init(&animation);
    lv_anim_set_var(&animation, icon);
    lv_anim_set_user_data(&animation, this);
    lv_anim_set_values(&animation, lv_obj_get_x(icon), -direction * _icon_transition_distance);
    lv_anim_set_duration(&animation, _smooth_motion ? 80 : 40);
    lv_anim_set_path_cb(&animation, lv_anim_path_ease_in);
    lv_anim_set_exec_cb(&animation, &LauncherView::icon_animation_set_x);
    lv_anim_set_completed_cb(&animation, &LauncherView::icon_animation_phase_one_completed);
    lv_anim_start(&animation);
}

void LauncherView::start_icon_enter_phase()
{
    if (_app_props.empty() || _icon_panels.empty()) {
        finish_icon_transition();
        return;
    }

    const int count = static_cast<int>(_app_props.size());
    _current_index = (_current_index + _transition_direction + count) % count;
    apply_current_icon();

    lv_obj_t* icon = _icon_panels.front()->get();
    lv_obj_set_x(icon, _transition_direction * _icon_transition_distance);
    lv_anim_t animation;
    lv_anim_init(&animation);
    lv_anim_set_var(&animation, icon);
    lv_anim_set_user_data(&animation, this);
    lv_anim_set_values(&animation, _transition_direction * _icon_transition_distance, 0);
    lv_anim_set_duration(&animation, _smooth_motion ? 140 : 60);
    lv_anim_set_path_cb(&animation, lv_anim_path_ease_out);
    lv_anim_set_exec_cb(&animation, &LauncherView::icon_animation_set_x);
    lv_anim_set_completed_cb(&animation, &LauncherView::icon_animation_phase_two_completed);
    lv_anim_start(&animation);
}

void LauncherView::finish_icon_transition()
{
    _transition_running = false;
    _transition_direction = 0;
    if (!_icon_panels.empty()) {
        lv_obj_set_x(_icon_panels.front()->get(), 0);
    }

    const int queued_direction = _queued_direction;
    _queued_direction = 0;
    if (queued_direction != 0) {
        begin_icon_transition(queued_direction);
    }
}

void LauncherView::apply_current_icon()
{
    if (_app_props.empty() || _icon_images.empty()) {
        return;
    }
    const auto& props = _app_props[_current_index];
    if (props.info.icon != nullptr) {
        _icon_images.front()->setSrc(props.info.icon);
    }
    _page_indicator->jumpTo(_current_index);
    _dynamic_icon_label->jumpTo(_current_index);
}

void LauncherView::handle_state_startup()
{
    _state = STATE_NORMAL;
}

void LauncherView::handle_state_normal()
{
    if (_clicked_app_id != -1) {
        if (onAppClicked) {
            onAppClicked(_clicked_app_id);
        }
        _clicked_app_id = -1;
    }

    if (_clock) {
        _clock->update();
    }
}
