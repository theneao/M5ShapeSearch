/*
 * 创建时间：2026-08-28
 * 作用：实现红色手绘/蓝色匹配缩略图和仅绘制可视窗口的 K 线图。
 * 使用方式：所有方法需在 LVGL 锁内调用；网络线程不能直接更新这些组件。
 * 修改时间：2026-08-29
 * 修改作用：详情 K 线每次载入重置为完整窗口，缩放范围扩展到 8 根并按比例变化，增强视觉区分度。
 */
#include "chart_widgets.h"

#include <algorithm>
#include <cmath>

namespace stock_selector {
namespace {

void drawLine(lv_layer_t* layer, float x1, float y1, float x2, float y2, lv_color_t color, int width,
              lv_opa_t opacity = LV_OPA_COVER)
{
    lv_draw_line_dsc_t descriptor;
    lv_draw_line_dsc_init(&descriptor);
    descriptor.p1.x = x1;
    descriptor.p1.y = y1;
    descriptor.p2.x = x2;
    descriptor.p2.y = y2;
    descriptor.color = color;
    descriptor.width = width;
    descriptor.opa = opacity;
    descriptor.round_start = true;
    descriptor.round_end = true;
    lv_draw_line(layer, &descriptor);
}

void drawRect(lv_layer_t* layer, const lv_area_t& area, lv_color_t color, lv_opa_t opacity = LV_OPA_COVER)
{
    lv_draw_rect_dsc_t descriptor;
    lv_draw_rect_dsc_init(&descriptor);
    descriptor.bg_color = color;
    descriptor.bg_opa = opacity;
    descriptor.border_width = 0;
    descriptor.radius = 2;
    lv_draw_rect(layer, &descriptor, &area);
}

void drawNormalized(
    lv_layer_t* layer,
    const lv_area_t& area,
    const std::vector<NormalizedPoint>& points,
    lv_color_t color,
    int width,
    lv_opa_t opacity
)
{
    if (points.size() < 2) {
        return;
    }
    const float draw_width = static_cast<float>(lv_area_get_width(&area) - 12);
    const float draw_height = static_cast<float>(lv_area_get_height(&area) - 12);
    for (std::size_t index = 1; index < points.size(); ++index) {
        drawLine(
            layer,
            area.x1 + 6 + std::clamp(points[index - 1].x, 0.0f, 1.0f) * draw_width,
            area.y1 + 6 + (1.0f - std::clamp(points[index - 1].y, 0.0f, 1.0f)) * draw_height,
            area.x1 + 6 + std::clamp(points[index].x, 0.0f, 1.0f) * draw_width,
            area.y1 + 6 + (1.0f - std::clamp(points[index].y, 0.0f, 1.0f)) * draw_height,
            color,
            width,
            opacity
        );
    }
}

}  // namespace

LinePreview::LinePreview(
    lv_obj_t* parent,
    const std::vector<NormalizedPoint>& query,
    const std::vector<NormalizedPoint>& match
) : _query(query), _match(match)
{
    _object = lv_obj_create(parent);
    lv_obj_set_style_bg_color(_object, lv_color_hex(0x0C1424), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(_object, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_border_width(_object, 0, LV_PART_MAIN);
    lv_obj_set_style_pad_all(_object, 0, LV_PART_MAIN);
    lv_obj_remove_flag(_object, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_remove_flag(_object, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(_object, &LinePreview::eventHandler, LV_EVENT_DRAW_MAIN, this);
}

LinePreview::~LinePreview()
{
    if (_object != nullptr && lv_obj_is_valid(_object)) {
        lv_obj_delete(_object);
    }
    _object = nullptr;
}

void LinePreview::eventHandler(lv_event_t* event)
{
    auto* self = static_cast<LinePreview*>(lv_event_get_user_data(event));
    if (self != nullptr) {
        self->draw(lv_event_get_layer(event));
    }
}

void LinePreview::draw(lv_layer_t* layer)
{
    lv_area_t area;
    lv_obj_get_coords(_object, &area);
    for (int step = 1; step < 3; ++step) {
        const int y = area.y1 + step * lv_area_get_height(&area) / 3;
        drawLine(layer, area.x1 + 5, y, area.x2 - 5, y, lv_color_hex(0x27344A), 1, LV_OPA_40);
    }
    drawNormalized(layer, area, _query, lv_color_hex(0xFF625F), 2, LV_OPA_80);
    drawNormalized(layer, area, _match, lv_color_hex(0x3C82F6), 3, LV_OPA_COVER);
}

KlineChart::KlineChart(lv_obj_t* parent)
{
    _object = lv_obj_create(parent);
    lv_obj_set_style_bg_color(_object, lv_color_hex(0x0B1322), LV_PART_MAIN);
    lv_obj_set_style_bg_opa(_object, LV_OPA_COVER, LV_PART_MAIN);
    lv_obj_set_style_border_color(_object, lv_color_hex(0x263349), LV_PART_MAIN);
    lv_obj_set_style_border_width(_object, 1, LV_PART_MAIN);
    lv_obj_set_style_radius(_object, 20, LV_PART_MAIN);
    lv_obj_set_style_pad_all(_object, 0, LV_PART_MAIN);
    lv_obj_remove_flag(_object, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_remove_flag(_object, LV_OBJ_FLAG_GESTURE_BUBBLE);
    lv_obj_add_flag(_object, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(_object, &KlineChart::eventHandler, LV_EVENT_ALL, this);
}

KlineChart::~KlineChart()
{
    if (_object != nullptr && lv_obj_is_valid(_object)) {
        lv_obj_delete(_object);
    }
    _object = nullptr;
}

void KlineChart::setData(const KlineDetail& detail)
{
    _bars = detail.bars;
    _ohlc_exact = detail.ohlcExact;
    _right_offset = 0;
    _visible_count = std::min<int>(44, std::max<int>(1, _bars.size()));
    lv_obj_invalidate(_object);
}

void KlineChart::zoomIn()
{
    if (_bars.empty()) {
        return;
    }
    const int minimum = std::min<int>(8, _bars.size());
    const int next = std::min(_visible_count - 1,
                              static_cast<int>(std::floor(_visible_count * 0.75f)));
    _visible_count = std::max(minimum, next);
    _right_offset = std::clamp(
        _right_offset, 0, std::max(0, static_cast<int>(_bars.size()) - _visible_count)
    );
    lv_obj_invalidate(_object);
}

void KlineChart::zoomOut()
{
    if (_bars.empty()) {
        return;
    }
    const int maximum = static_cast<int>(_bars.size());
    const int next = std::max(_visible_count + 1,
                              static_cast<int>(std::ceil(_visible_count * 1.34f)));
    _visible_count = std::min(maximum, next);
    _right_offset = std::clamp(
        _right_offset, 0, std::max(0, static_cast<int>(_bars.size()) - _visible_count)
    );
    lv_obj_invalidate(_object);
}

int KlineChart::visibleCount() const
{
    return std::min<int>(_visible_count, _bars.size());
}

void KlineChart::eventHandler(lv_event_t* event)
{
    auto* self = static_cast<KlineChart*>(lv_event_get_user_data(event));
    if (self == nullptr) {
        return;
    }
    const lv_event_code_t code = lv_event_get_code(event);
    if (code == LV_EVENT_DRAW_MAIN) {
        self->draw(lv_event_get_layer(event));
    } else if (code == LV_EVENT_PRESSED || code == LV_EVENT_PRESSING || code == LV_EVENT_RELEASED ||
               code == LV_EVENT_PRESS_LOST) {
        self->handleTouch(code, lv_indev_active());
    }
}

void KlineChart::handleTouch(lv_event_code_t code, lv_indev_t* input)
{
    if (code == LV_EVENT_RELEASED || code == LV_EVENT_PRESS_LOST) {
        _dragging = false;
        return;
    }
    if (input == nullptr || _bars.empty()) {
        return;
    }
    lv_point_t point;
    lv_indev_get_point(input, &point);
    if (code == LV_EVENT_PRESSED) {
        _dragging = true;
        _drag_x = point.x;
        return;
    }
    if (!_dragging) {
        return;
    }
    const int slot = std::max(2, static_cast<int>(lv_obj_get_width(_object)) / std::max(1, _visible_count));
    const int delta = point.x - _drag_x;
    if (std::abs(delta) >= slot) {
        _right_offset = std::clamp(
            _right_offset + delta / slot,
            0,
            std::max(0, static_cast<int>(_bars.size()) - _visible_count)
        );
        _drag_x = point.x;
        lv_obj_invalidate(_object);
    }
}

void KlineChart::draw(lv_layer_t* layer)
{
    lv_area_t area;
    lv_obj_get_coords(_object, &area);
    const int left = area.x1 + 12;
    const int right = area.x2 - 12;
    const int top = area.y1 + 12;
    const int bottom = area.y2 - 12;
    for (int step = 1; step < 4; ++step) {
        const int y = top + step * (bottom - top) / 4;
        drawLine(layer, left, y, right, y, lv_color_hex(0x28364D), 1, LV_OPA_50);
    }
    if (_bars.size() < 2) {
        return;
    }

    const int end = std::max(1, static_cast<int>(_bars.size()) - _right_offset);
    const int start = std::max(0, end - _visible_count);
    float minimum = _bars[start].low;
    float maximum = _bars[start].high;
    for (int index = start; index < end; ++index) {
        minimum = std::min(minimum, _ohlc_exact ? _bars[index].low : _bars[index].close);
        maximum = std::max(maximum, _ohlc_exact ? _bars[index].high : _bars[index].close);
    }
    const float range = std::max(maximum - minimum, 1e-6f);
    const float chart_width = static_cast<float>(right - left);
    const float chart_height = static_cast<float>(bottom - top);
    const int count = std::max(1, end - start);
    const float slot = chart_width / static_cast<float>(count);
    auto mapY = [&](float value) {
        return bottom - (value - minimum) / range * chart_height;
    };

    // 详情页只做可见窗口内的轻量均线绘制，匹配特征仍全部由服务器计算。
    const auto drawMovingAverage = [&](int period, lv_color_t color) {
        bool has_previous = false;
        float previous_x = 0.0f;
        float previous_y = 0.0f;
        for (int index = start; index < end; ++index) {
            if (index + 1 < period) {
                continue;
            }
            float sum = 0.0f;
            for (int offset = 0; offset < period; ++offset) {
                sum += _bars[index - offset].close;
            }
            const float x = left + (index - start + 0.5f) * slot;
            const float y = mapY(sum / static_cast<float>(period));
            if (has_previous) {
                drawLine(layer, previous_x, previous_y, x, y, color, 2, LV_OPA_80);
            }
            previous_x = x;
            previous_y = y;
            has_previous = true;
        }
    };

    if (!_ohlc_exact) {
        for (int index = start + 1; index < end; ++index) {
            const float x1 = left + (index - start - 0.5f) * slot;
            const float x2 = left + (index - start + 0.5f) * slot;
            drawLine(layer, x1, mapY(_bars[index - 1].close), x2, mapY(_bars[index].close),
                     lv_color_hex(0x3C82F6), 3);
        }
        drawMovingAverage(5, lv_color_hex(0xF6C85F));
        drawMovingAverage(10, lv_color_hex(0xA56EFF));
        drawMovingAverage(20, lv_color_hex(0x58C7F3));
        return;
    }

    for (int index = start; index < end; ++index) {
        const KlineBar& bar = _bars[index];
        const int center = static_cast<int>(left + (index - start + 0.5f) * slot);
        const lv_color_t color = bar.close >= bar.open ? lv_color_hex(0x31D0AA) : lv_color_hex(0xFF667A);
        drawLine(layer, center, mapY(bar.high), center, mapY(bar.low), color, 1);
        const int half = std::max(1, static_cast<int>(slot * 0.32f));
        lv_area_t body = {
            center - half,
            static_cast<int32_t>(std::min(mapY(bar.open), mapY(bar.close))),
            center + half,
            static_cast<int32_t>(std::max(mapY(bar.open), mapY(bar.close))),
        };
        if (body.y2 <= body.y1) {
            body.y2 = body.y1 + 1;
        }
        drawRect(layer, body, color);
    }

    drawMovingAverage(5, lv_color_hex(0xF6C85F));
    drawMovingAverage(10, lv_color_hex(0xA56EFF));
    drawMovingAverage(20, lv_color_hex(0x58C7F3));
}

}  // namespace stock_selector
