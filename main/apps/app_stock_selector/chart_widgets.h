/*
 * 创建时间：2026-08-28
 * 作用：提供结果缩略图和详情 K 线的局部自定义绘制，避免创建大量 LVGL 子对象。
 * 使用方式：LinePreview 用于结果卡片；KlineChart 用于单标的详情并支持拖动/缩放。
 * 修改时间：2026-08-29
 * 修改作用：K 线缩放改为比例缩放并开放可见/总根数，供详情页显示明确的缩放反馈。
 * 修改时间：2026-08-30
 * 修改作用：K 线横向拖动增加像素级跟手位移与松手吸附，避免逐根跳动造成卡顿感。
 * 修改时间：2026-08-31
 * 修改作用：LinePreview 对两条对比曲线独立拉伸到完整缩略图绘图区，视觉规则与 Web 缩略图一致。
 */
#pragma once

#include "stock_types.h"
#include <lvgl.h>
#include <vector>

namespace stock_selector {

class LinePreview {
public:
    LinePreview(
        lv_obj_t* parent,
        const std::vector<NormalizedPoint>& query,
        const std::vector<NormalizedPoint>& match
    );
    ~LinePreview();
    lv_obj_t* object() const { return _object; }

private:
    static void eventHandler(lv_event_t* event);
    void draw(lv_layer_t* layer);
    lv_obj_t* _object = nullptr;
    std::vector<NormalizedPoint> _query;
    std::vector<NormalizedPoint> _match;
};

class KlineChart {
public:
    explicit KlineChart(lv_obj_t* parent);
    ~KlineChart();
    lv_obj_t* object() const { return _object; }

    void setData(const KlineDetail& detail);
    void zoomIn();
    void zoomOut();
    int visibleCount() const;
    int totalCount() const { return static_cast<int>(_bars.size()); }

private:
    static void eventHandler(lv_event_t* event);
    void draw(lv_layer_t* layer);
    void handleTouch(lv_event_code_t code, lv_indev_t* input);

    lv_obj_t* _object = nullptr;
    std::vector<KlineBar> _bars;
    bool _ohlc_exact = false;
    int _visible_count = 44;
    int _right_offset = 0;
    int _drag_x = 0;
    int _pan_pixels = 0;
    bool _dragging = false;
};

}  // namespace stock_selector
