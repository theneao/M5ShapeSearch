/*
 * 创建时间：2026-08-28
 * 作用：接入 Mooncake 生命周期，协调 LVGL 主线程与后台 HTTP 服务。
 * 修改时间：2026-08-29
 * 修改作用：左 A 键统一取消/返回，右 B 键执行页面主操作，所有形态子页支持左滑返回。
 * 使用方式：在 main.cpp 安装 AppStockSelector；按 A 取消/返回，按 B 匹配/打开 Top1，双键长按回首页。
 *
 * 修改时间：2026-08-29
 * 修改作用：缺失周期后台建库时显示等待/取消提示，不把正常建库过程误报成请求卡死。
 */
#pragma once

#include "shape_match_service.h"
#include "view.h"
#include <apps/common/key_manager/key_manager.h>
#include <mooncake.h>
#include <memory>

class AppStockSelector : public mooncake::AppAbility {
public:
    AppStockSelector();
    void onCreate() override;
    void onOpen() override;
    void onRunning() override;
    void onClose() override;

private:
    stock_selector::ShapeMatchService _service;
    std::unique_ptr<stock_selector::StockSelectorView> _view;
    std::unique_ptr<input::KeyManager> _key_manager;
    bool _touch_active = false;
    int _touch_start_x = 0;
    int _touch_start_y = 0;
    int _touch_last_x = 0;
    int _touch_last_y = 0;
    bool _waiting_data_shown = false;
};
