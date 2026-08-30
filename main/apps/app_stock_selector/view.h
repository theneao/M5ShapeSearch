/*
 * 创建时间：2026-08-28
 * 作用：定义形态搜索的画板、Top-K 结果和单标的详情三页 LVGL 界面。
 * 修改时间：2026-08-29
 * 修改作用：移除应用内部设置页，WiFi、服务器和数据设置统一迁入整机 Settings。
 * 使用方式：App 生命周期内 init/update；右键调用 triggerPrimary()，左键/左滑调用 goBack()。
 * 修改时间：2026-08-29
 * 修改作用：详情页增加 K 线缩放根数提示，使 +/- 操作有可核对的即时反馈。
 */
#pragma once

#include "chart_widgets.h"
#include "draw_canvas.h"
#include "stock_types.h"

#include <lvgl.h>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace stock_selector {

class StockSelectorView {
public:
    enum class Page { Draw, Results, Detail };

    StockSelectorView() = default;
    ~StockSelectorView();

    std::function<void(const std::vector<NormalizedPoint>&, const std::string&, const std::string&)>
        onMatchRequested;
    std::function<void(const MatchResult&, const std::string&)> onDetailRequested;

    void init(lv_obj_t* parent);
    void update(uint32_t now, const std::string& networkStatus, int rssi);
    void showResults(const std::vector<MatchResult>& results, int queryMs);
    void setDetail(const KlineDetail& detail);
    void setBusy(bool busy, const char* text = "SEARCHING SERVER");
    void showError(const std::string& message);
    bool goBack();
    void triggerPrimary();
    bool isBusy() const { return _busy; }
    Page page() const { return _current_page; }

private:
    enum Action {
        ActionBack,
        ActionClear,
        ActionMatch,
        ActionCategory,
        ActionTimeframe,
        ActionResult,
        ActionZoomOut,
        ActionZoomIn,
    };
    struct ButtonBinding {
        StockSelectorView* view = nullptr;
        Action action = ActionBack;
        int value = 0;
    };

    static void buttonEvent(lv_event_t* event);
    void handleAction(Action action, int value);
    void createDrawPage(bool animate = true);
    void createResultsPage(bool animate = true);
    void createDetailPage(bool animate = true);
    void destroyPage();
    void animatePageIn(int fromX);

    lv_obj_t* createButton(
        lv_obj_t* parent,
        const char* text,
        int x,
        int y,
        int width,
        int height,
        uint32_t color,
        Action action,
        int value = 0
    );
    lv_obj_t* createLabel(
        lv_obj_t* parent,
        const char* text,
        const lv_font_t* font,
        uint32_t color,
        lv_align_t align,
        int x,
        int y
    );
    void styleChipSelection();
    void updateBusyAnimation(uint32_t now);
    void updateZoomLabel();
    static std::string asciiOrFallback(const std::string& text, const std::string& fallback);

    lv_obj_t* _root = nullptr;
    lv_obj_t* _page = nullptr;
    lv_obj_t* _network_label = nullptr;
    lv_obj_t* _wifi_icon = nullptr;
    lv_obj_t* _hint_label = nullptr;
    lv_obj_t* _busy_overlay = nullptr;
    lv_obj_t* _busy_label = nullptr;
    lv_obj_t* _scan_line = nullptr;
    lv_obj_t* _toast = nullptr;
    lv_obj_t* _detail_state_label = nullptr;
    lv_obj_t* _zoom_label = nullptr;
    std::unique_ptr<DrawCanvas> _draw_canvas;
    std::vector<std::unique_ptr<LinePreview>> _previews;
    std::unique_ptr<KlineChart> _kline_chart;
    std::vector<std::unique_ptr<ButtonBinding>> _bindings;
    std::vector<lv_obj_t*> _category_buttons;
    lv_obj_t* _timeframe_button = nullptr;

    Page _current_page = Page::Draw;
    std::string _category = "all";
    std::string _timeframe = "1d";
    std::vector<NormalizedPoint> _query_points;
    std::vector<MatchResult> _results;
    MatchResult _selected;
    int _query_ms = 0;
    bool _busy = false;
    std::string _busy_text;
    uint32_t _busy_started = 0;
    uint32_t _last_busy_second = UINT32_MAX;
    uint32_t _last_network_update = 0;
    uint32_t _toast_until = 0;
};

}  // namespace stock_selector
