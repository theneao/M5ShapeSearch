/*
 * SPDX-FileCopyrightText: 2026 M5Stack Technology CO LTD
 *
 * SPDX-License-Identifier: MIT
 *
 * 修改时间：2026-08-29
 * 修改作用：设置菜单进入/返回增加可配置的 ease-out 位移与淡入，减少页面突跳。
 *
 * 修改时间：2026-08-30
 * 修改作用：参考 x-track 页面管理器，仅动画根节点坐标，取消全屏透明度混合，降低圆屏切页重绘成本。
 * 使用方式：Motion=Smooth 使用 240ms ease-out；Eco 使用 100ms。
 *
 * 修改时间：2026-08-30
 * 修改作用：466×466 QSPI 面板不再逐帧移动全屏设置容器，改为单帧切页，消除带宽不足造成的卡顿。
 * 使用方式：菜单进入和返回立即响应；首页图标仍保留小区域动画反馈。
 */
#include "view.h"
#include <assets/assets.h>

using namespace view;
using namespace uitk::lvgl_cpp;

SelectMenuPage::SelectMenuPage(std::vector<MenuSection> sections) : _sections(std::move(sections))
{
    _pannel = std::make_unique<uitk::lvgl_cpp::Container>(lv_screen_active());
    _pannel->setSize(466, 466);
    _pannel->setBgColor(lv_color_black());
    _pannel->setPadding(0, 72, 0, 0);
    _pannel->setBorderWidth(0);
    _pannel->setRadius(0);
    _pannel->setScrollDir(LV_DIR_VER);
    _pannel->setScrollbarMode(LV_SCROLLBAR_MODE_ACTIVE);

    int cursor_y = 36;

    for (int i = 0; i < _sections.size(); ++i) {
        const auto& section = _sections[i];
        create_selection_label(cursor_y, section.title);
        cursor_y += 28 + 24;

        for (int j = 0; j < section.items.size(); ++j) {
            const auto& item = section.items[j];
            create_item_button(cursor_y, item, i, j);
            cursor_y += 119 + 21;
        }
        cursor_y += 5;
    }
    cursor_y += 20;

    // 整页位移会让 466×466 的脏区在每一帧都覆盖全屏；在 QSPI 面板上
    // 单次稳定提交比低帧率的全屏动画更快，也不会出现上下割裂。
    lv_obj_set_x(_pannel->get(), 0);
}

void SelectMenuPage::update()
{
    if (_pending_section_index >= 0 && _pending_item_index >= 0) {
        if (_pending_section_index < _sections.size()) {
            auto& section = _sections[_pending_section_index];
            if (_pending_item_index < section.items.size()) {
                auto& item = section.items[_pending_item_index];
                if (item.onClick) {
                    item.onClick();
                }
            }
        }
        _pending_section_index = -1;
        _pending_item_index    = -1;
    }
}

void SelectMenuPage::create_selection_label(int y, std::string_view text)
{
    auto label = std::make_unique<uitk::lvgl_cpp::Label>(*_pannel);
    label->setText(text);
    label->setTextFont(&MontserratSemiBold26);
    label->setTextColor(lv_color_hex(0xFFFFFF));
    label->align(LV_ALIGN_TOP_MID, 0, y);
    _labels.push_back(std::move(label));
}

void SelectMenuPage::create_item_button(int y, const MenuItem& item, int section_idx, int item_idx)
{
    auto btn = std::make_unique<uitk::lvgl_cpp::Button>(*_pannel);
    btn->setSize(374, 119);
    btn->align(LV_ALIGN_TOP_MID, 0, y);
    btn->setBgColor(lv_color_hex(0x4C4C4C));
    btn->setBorderWidth(0);
    btn->setShadowWidth(0);
    btn->setRadius(60);

    btn->label().setText(item.label);
    btn->label().setTextFont(&lv_font_montserrat_28);
    btn->label().setTextColor(lv_color_hex(0xFFFFFF));
    btn->label().align(LV_ALIGN_CENTER, 0, 0);
    btn->label().setWidth(294);
    btn->label().setTextAlign(LV_TEXT_ALIGN_CENTER);
    btn->label().setLongMode(LV_LABEL_LONG_MODE_SCROLL_CIRCULAR);

    if (item.onClick) {
        btn->onClick().connect([this, section_idx, item_idx]() {
            _pending_section_index = section_idx;
            _pending_item_index    = item_idx;
        });
    }
    _buttons.push_back(std::move(btn));
}
