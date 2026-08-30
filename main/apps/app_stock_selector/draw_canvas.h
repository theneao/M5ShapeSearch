/*
 * 创建时间：2026-08-28
 * 作用：提供 60Hz 级触摸采样和 LVGL 自定义曲线绘制，输出最多 128 个归一化点。
 * 使用方式：创建 DrawCanvas(parent)，MATCH 时调用 normalizedPoints() 发送给服务器。
 */
#pragma once

#include "stock_types.h"
#include <lvgl.h>
#include <cstdint>
#include <vector>

namespace stock_selector {

class DrawCanvas {
public:
    explicit DrawCanvas(lv_obj_t* parent);
    ~DrawCanvas();

    lv_obj_t* object() const { return _object; }
    void clear();
    bool empty() const { return _points.size() < 3; }
    std::vector<NormalizedPoint> normalizedPoints(std::size_t maxPoints = 128) const;

private:
    struct RawPoint {
        int16_t x = 0;
        int16_t y = 0;
    };

    static void eventHandler(lv_event_t* event);
    void handleTouch(lv_event_code_t code, lv_indev_t* input);
    void draw(lv_layer_t* layer);

    lv_obj_t* _object = nullptr;
    std::vector<RawPoint> _points;
    uint32_t _last_sample_tick = 0;
    bool _drawing = false;
};

}  // namespace stock_selector
