/*
 * 创建时间：2026-08-28
 * 作用：实现形态搜索 App 生命周期，服务器请求不占用 LVGL 线程。
 * 修改时间：2026-08-29
 * 修改作用：移除应用内配网入口；网络与数据配置统一由整机 Settings 管理。
 * 使用方式：从启动器进入 Shape Search；服务器需监听局域网 8000 端口。
 *
 * 修改时间：2026-08-29
 * 修改作用：服务端按需构建缺失周期时显示等待状态；A/左滑可放弃，服务器仍继续建库。
 *
 * 修改时间：2026-08-31
 * 修改作用：详情页加载期间按 A/左滑会同时取消 HTTP 请求并立即返回结果页；
 *           不再要求先取消、再按一次返回，避免连续按键穿透多级页面并误关闭应用。
 * 使用方式：详情页任意状态按一次 A 或左滑即可返回；绘图首页 A 才退出应用。
 *
 * 修改时间：2026-08-31
 * 修改作用：手势检测改读 LVGL 已采样的触控状态，不再与 LVGL 任务并发访问 CST820/I2C，修复匹配和页面操作期间的触控竞态重启风险。
 * 使用方式：交互方式不变；左滑仍用于返回或取消。
 */
#include "app_stock_selector.h"

#include <assets/assets.h>
#include <hal/hal.h>
#include <mooncake_log.h>
#include <cmath>

AppStockSelector::AppStockSelector()
{
    setAppInfo().name = "Shape Search";
    setAppInfo().icon = (void*)&icon_fft;
}

void AppStockSelector::onCreate()
{
    mclog::tagInfo(getAppInfo().name, "on create");
}

void AppStockSelector::onOpen()
{
    _service.initializeNetwork();
    mclog::tagInfo(getAppInfo().name, "on open, API: {}", _service.serverUrl());
    _key_manager = std::make_unique<input::KeyManager>();
    LvglLockGuard lock;
    _view = std::make_unique<stock_selector::StockSelectorView>();
    _view->init(lv_screen_active());
    _view->onMatchRequested = [this](
        const std::vector<stock_selector::NormalizedPoint>& points,
        const std::string& category,
        const std::string& timeframe
    ) {
        if (!_service.isNetworkConnected()) {
            _view->showError("WiFi offline; open system Settings");
            return;
        }
        if (_service.submitMatch(points, category, timeframe, 10)) {
            _view->setBusy(true, "MATCHING ON SERVER");
        } else {
            _view->showError("Request queue is busy");
        }
    };
    _view->onDetailRequested = [this](
        const stock_selector::MatchResult& result,
        const std::string& timeframe
    ) {
        if (!_service.submitDetail(result, timeframe, 200)) {
            _view->showError("Detail request queue is busy");
        }
    };
    _view->onStrategyCatalogRequested = [this]() {
        if (!_service.isNetworkConnected()) {
            _view->showError("WiFi offline; open system Settings");
            return;
        }
        if (!_service.submitStrategyCatalog()) {
            _view->showError("Strategy request queue is busy");
        }
    };
    _view->onStrategyScreenRequested = [this](
        const std::vector<std::string>& ids,
        bool intersection,
        const std::string& category,
        const std::string& timeframe
    ) {
        if (_service.submitStrategyScreen(ids, intersection, category, timeframe, 20)) {
            _view->setBusy(true, "FILTERING ON SERVER");
        } else {
            _view->showError("Strategy request queue is busy");
        }
    };
    _view->onSaveStrategyRequested = [this](
        const std::vector<stock_selector::NormalizedPoint>& points
    ) {
        if (_service.submitSaveStrategy(points, 0.68f)) {
            _view->setBusy(true, "SAVING ON SERVER");
        } else {
            _view->showError("Save request queue is busy");
        }
    };
}

void AppStockSelector::onRunning()
{
    GetHAL().updateButtonStates();
    const input::KeyEvent event = _key_manager ? _key_manager->update(false) : input::KeyEvent::None;
    _service.pollTimeout();
    const bool waiting_data = _service.state() == stock_selector::ShapeMatchService::State::WaitingData;

    bool swipe_left = false;
    {
        LvglLockGuard touch_lock;
        lv_indev_t* touchpad = GetHAL().lvTouchpad;
        lv_point_t point{};
        const bool pressed = touchpad != nullptr &&
            lv_indev_get_state(touchpad) == LV_INDEV_STATE_PRESSED;
        if (pressed) {
            lv_indev_get_point(touchpad, &point);
            if (!_touch_active) {
                _touch_active = true;
                _touch_start_x = point.x;
                _touch_start_y = point.y;
            }
            _touch_last_x = point.x;
            _touch_last_y = point.y;
        } else if (_touch_active) {
            const int dx = _touch_last_x - _touch_start_x;
            const int dy = _touch_last_y - _touch_start_y;
            swipe_left = dx <= -90 && std::abs(dy) <= 75 &&
                (_service.isBusy() || (_view && _view->page() != stock_selector::StockSelectorView::Page::Draw));
            _touch_active = false;
        }
    }

    const bool back_or_cancel = event == input::KeyEvent::GoPrevious || swipe_left;
    const bool go_home = event == input::KeyEvent::GoHome;
    const bool cancelled_request = (back_or_cancel || go_home) && _service.isBusy() &&
        _service.cancelCurrent(go_home ? "Request cancelled; returning home" : "Request cancelled");

    bool close_requested = false;
    {
        LvglLockGuard lock;
        if (go_home) {
            close_requested = true;
        } else if (back_or_cancel) {
            // 子页的返回优先级高于请求取消：一次 A/左滑应同时完成两件事。
            // 绘图首页仍保留“忙时只取消，空闲时退出”的原有行为。
            const bool on_child_page = _view->page() != stock_selector::StockSelectorView::Page::Draw;
            if (on_child_page) {
                close_requested = !_view->goBack();
            } else if (!cancelled_request) {
                close_requested = true;
            }
        } else if (event == input::KeyEvent::GoNext) {
            _view->triggerPrimary();
        }

        if (waiting_data && !_waiting_data_shown) {
            _view->setBusy(true, "SERVER BUILDING DATA\nA / BACK: CANCEL");
            _waiting_data_shown = true;
        } else if (!waiting_data) {
            _waiting_data_shown = false;
        }

        std::vector<stock_selector::MatchResult> results;
        std::vector<stock_selector::StrategyDefinition> strategies;
        int query_ms = 0;
        stock_selector::KlineDetail detail;
        std::string error;
        if (_service.takeMatchResults(results, query_ms)) {
            _view->showResults(results, query_ms);
        } else if (_service.takeStrategyCatalog(strategies)) {
            _view->setStrategies(strategies);
        } else if (_service.takeStrategyResults(results, query_ms)) {
            _view->showStrategyResults(results, query_ms);
        } else if (_service.takeStrategySaved()) {
            _view->showStrategySaved();
        } else if (_service.takeDetail(detail)) {
            _view->setDetail(detail);
        } else if (_service.takeError(error)) {
            _view->showError(error);
        }
        _view->update(GetHAL().millis(), _service.networkStatus(), _service.networkRssi());
    }
    if (close_requested) {
        close();
    }
}

void AppStockSelector::onClose()
{
    mclog::tagInfo(getAppInfo().name, "on close");
    _service.cancelCurrent("App closed");
    _waiting_data_shown = false;
    _touch_active = false;
    _key_manager.reset();
    LvglLockGuard lock;
    _view.reset();
}
