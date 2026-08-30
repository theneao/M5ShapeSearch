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
    const int show_range      = 150;
    const int pos_y           = 155;
    const int transition_zone = 80;

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

        // Calculate current icon index and distance to icon center
        _current_index        = (scrollValue + _icon_gap / 2) / _icon_gap;
        int icon_center_pos_x = _current_index * _icon_gap;
        int distance_to_icon  = std::abs(scrollValue - icon_center_pos_x);

        // Clamp index
        if (_current_index < 0) {
            _current_index = 0;
        }
        if (_current_index >= _icon_label_texts.size()) {
            _current_index = _icon_label_texts.size() - 1;
        }

        // Check if label should be visible
        bool should_be_visible = (distance_to_icon <= show_range);

        // If index changed, update label text
        if (_last_index != _current_index) {
            _label->setText(_icon_label_texts[_current_index]);
        }

        // Update opacity based on distance when in transition zone
        if (should_be_visible && distance_to_icon > (show_range - transition_zone)) {
            // Fade out as approaching edge
            float fade_ratio = 1.0f - (float)(distance_to_icon - (show_range - transition_zone)) / transition_zone;
            set_opacity(static_cast<lv_opa_t>(255 * fade_ratio));
        } else if (should_be_visible) {
            set_opacity(255);
        }
    }

private:
    std::vector<std::string> _icon_label_texts;
    int _icon_gap      = 0;
    int _current_index = 0;
    int _last_index    = 0;
    bool _is_visible   = false;
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
// 3 组足够覆盖左右循环：[0:左备用] [1:当前] [2:右备用]。
static constexpr int _loop_copies       = 3;
static constexpr int _center_copy_index = 1;

static int _last_clicked_icon_pos_x = -1;
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
    _panel->setScrollbarMode(LV_SCROLLBAR_MODE_OFF);
    _panel->setBgColor(lv_color_hex(0x000000));
    _panel->addFlag(LV_OBJ_FLAG_SCROLL_ONE);
    _panel->setPaddingAll(0);
    lv_obj_set_scroll_snap_x(_panel->get(), LV_SCROLL_SNAP_CENTER);
    lv_obj_add_event_cb(_panel->get(), &LauncherView::scroll_begin_event_cb, LV_EVENT_SCROLL_BEGIN, this);

    /* ---------------------------------- Icons --------------------------------- */
    int icon_x = 0;
    int icon_y = -15;
    std::vector<std::string> icon_label_texts;
    std::vector<uint32_t> step_colors;

    // Loop multiple times to create fake infinite scroll
    for (int loop = 0; loop < _loop_copies; loop++) {
        for (const auto& props : appPorps) {
            // Icon panel
            _icon_panels.push_back(std::make_unique<Container>(_panel->get()));
            _icon_panels.back()->setAlign(LV_ALIGN_CENTER);
            _icon_panels.back()->setSize(200, 200);
            _icon_panels.back()->setPos(icon_x, icon_y);
            _icon_panels.back()->setBorderWidth(0);
            _icon_panels.back()->removeFlag(LV_OBJ_FLAG_SCROLLABLE);
            _icon_panels.back()->setBgOpa(0);

            // Icon click callback
            auto app_id = props.appID;
            auto pos_x  = icon_x;
            _icon_panels.back()->onClick().connect([&, app_id, pos_x]() {
                _clicked_app_id          = app_id;
                _last_clicked_icon_pos_x = pos_x;
            });

            // Keep track of data for helpers
            icon_label_texts.push_back(props.info.name);

            uint32_t color = 0xDADADA;
            if (props.info.userData != nullptr) {
                color = *(uint32_t*)props.info.userData;
            }
            step_colors.push_back(color);

            // Icon image
            if (props.info.icon != nullptr) {
                _icon_images.push_back(std::make_unique<Image>(_icon_panels.back()->get()));
                _icon_images.back()->setSrc(props.info.icon);
                _icon_images.back()->setAlign(LV_ALIGN_CENTER);
            }

            icon_x += _icon_gap;
        }
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
    _page_indicator->init(appPorps.size(), _icon_gap, _panel->get(), 0, 200);

    /* --------------------------- Dynamic icon label --------------------------- */
    _dynamic_icon_label = std::make_unique<DynamicIconLabel>();
    _dynamic_icon_label->init(icon_label_texts, _icon_gap, _panel->get());

    /* ---------------------------------- Clock --------------------------------- */
    _clock = std::make_unique<view::ArcTopClock>(_panel->get());
    _clock->init();
    _clock->align(LV_ALIGN_TOP_MID, 0, 4);
    _clock->addFlag(LV_OBJ_FLAG_FLOATING);

    /* ----------------------------- History restore ---------------------------- */
    bool need_restore      = false;
    int restore_icon_pos_x = -1;

    // Normal start pos (Center of the repeated sets)
    int base_offset_rounds = _center_copy_index * appPorps.size();
    int default_start_x    = base_offset_rounds * _icon_gap;

    // // If warm boot was requested
    // if (GetHAL().getWarmRebootTarget() >= 0) {
    //     auto app_index = GetHAL().getWarmRebootTarget();
    //     mclog::tagInfo(_tag, "warm boot was requested, app index: {}", app_index);
    //     app_index = uitk::clamp(app_index, 0, static_cast<int>(appPorps.size()) - 1);

    //     // Restore to center set
    //     restore_icon_pos_x = (base_offset_rounds + app_index) * _icon_gap;
    //     need_restore       = true;
    //     GetHAL().clearWarmRebootRequest();
    // }

    int logical_restore_index = 0;
    if (_last_clicked_icon_pos_x != -1 && !appPorps.empty()) {
        // 历史位置可能来自任意循环副本，恢复时统一映射到中间组。
        logical_restore_index = (_last_clicked_icon_pos_x / _icon_gap) % appPorps.size();
        restore_icon_pos_x = (base_offset_rounds + logical_restore_index) * _icon_gap;
        need_restore             = true;
        _last_clicked_icon_pos_x = -1;
    }

    if (!need_restore) {
        restore_icon_pos_x = default_start_x;
    }
    _panel->scrollBy(-restore_icon_pos_x, 0, LV_ANIM_OFF);

    _page_indicator->jumpTo(logical_restore_index);
    _dynamic_icon_label->jumpTo(base_offset_rounds + logical_restore_index);

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
    auto current_scroll_x = _panel->getScrollX();
    int current_index     = (current_scroll_x + _icon_gap / 2) / _icon_gap;
    int target_index      = current_index + direction;

    int target_x        = target_index * _icon_gap;
    // 使用 LVGL 内部 scroll_x_anim；其每帧直接移动子对象，不再重复发送
    // SCROLL_BEGIN/SCROLL_END 事件和执行边界、吸附计算。
    lv_obj_scroll_to_x(_panel->get(), target_x, LV_ANIM_ON);
}

void LauncherView::scroll_begin_event_cb(lv_event_t* event)
{
    auto* animation = static_cast<lv_anim_t*>(lv_event_get_param(event));
    auto* self      = static_cast<LauncherView*>(lv_event_get_user_data(event));
    if (animation == nullptr || self == nullptr) {
        return;
    }
    lv_anim_set_duration(animation, self->_smooth_motion ? 300 : 120);
    lv_anim_set_path_cb(animation, lv_anim_path_ease_out);
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

    handle_scroll_in_loop();

    int scroll_x = _panel->getScrollX();
    // mclog::tagInfo(_tag, "scroll x: {}", scroll_x);

    _page_indicator->update(scroll_x);
    _dynamic_icon_label->update(scroll_x);

    if (_clock) {
        _clock->update();
    }
}

void LauncherView::handle_scroll_in_loop()
{
    // We get total size from underlying icons count / copies
    int total_icons   = _icon_panels.size();
    int icons_per_set = total_icons / _loop_copies;
    int set_width_px  = icons_per_set * _icon_gap;

    int current_scroll_x = _panel->getScrollX();

    // 当前组固定为中间 Copy 1；进入左右备用组后瞬移回等价位置。
    int center_set_start_x = _center_copy_index * set_width_px;
    int left_trigger_limit  = center_set_start_x;
    int right_trigger_limit = center_set_start_x + set_width_px;

    // Wrap-around Logic
    // Only perform teleport if we are NOT in an automated scroll animation
    // (To avoid interrupting the snap/scroll-to animation which would leave us stuck between icons)
    // However, if the user is manually dragging (PRESSED), we MUST teleport to allow infinite drag.
    bool is_auto_scrolling = lv_obj_is_scrolling(_panel->get()) && !lv_obj_has_state(_panel->get(), LV_STATE_PRESSED);

    if (!is_auto_scrolling) {
        if (current_scroll_x < left_trigger_limit) {
            // Too far left (Set 1), warp right to Set 2
            // scrollBy(-val) increases scroll_x
            _panel->scrollBy(-set_width_px, 0, LV_ANIM_OFF);
        } else if (current_scroll_x >= right_trigger_limit) {
            // Too far right (Set 3), warp left to Set 2
            // scrollBy(+val) decreases scroll_x
            _panel->scrollBy(set_width_px, 0, LV_ANIM_OFF);
        }
    }
}
