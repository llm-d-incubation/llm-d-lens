// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
    Activity,
    AlertTriangle,
    Clock3,
    GitBranch,
    Layers,
    Search,
} from 'lucide-react';
import {
    Bar,
    BarChart,
    CartesianGrid,
    Line,
    LineChart,
    ReferenceLine,
    ResponsiveContainer,
    Tooltip,
    XAxis,
    YAxis,
} from 'recharts';
import { cn } from '../../../../shared/lib/cn';
import {
    Badge,
    Button,
    CHART_SERIES,
    ChartContainer,
    ChartLegend,
    ChartTooltip,
    ChartTooltipRow,
    EmptyState,
    HelpTooltip,
    Panel,
    ToggleGroup,
} from '../../../../shared/ui';
import {
    displayName,
    formatCount,
    formatDecimal,
    formatDriftMs,
    formatErrorCategory,
    formatMilliseconds,
    formatNumber,
    numberAt,
    responseStatusGuidance,
} from '../../model/presentation';
import { LatencyHeatmap } from './LatencyHeatmap';

const AXIS_TICK = { fill: 'var(--text-muted)', fontSize: 10 };
const BAR_TOOLTIP_CURSOR = { fill: 'var(--chart-cursor-fill, rgba(148, 163, 184, 0.10))' };
const LINE_TOOLTIP_CURSOR = { stroke: 'var(--chart-cursor-stroke, rgba(148, 163, 184, 0.28))' };
const PROMPT_BUCKET_ORDER = ['<512', '512-2K', '2K-8K', '8K-32K', '>32K'];
const CACHE_BUCKET_ORDER = ['cold', 'partial', 'warm'];

function firstFinite(...values) {
    for (const value of values) {
        if (value === null || value === undefined || typeof value === 'boolean') continue;
        if (typeof value === 'string' && value.trim() === '') continue;
        const numeric = Number(value);
        if (Number.isFinite(numeric)) return numeric;
    }
    return null;
}

function normalizeCacheClass(raw) {
    if (raw === 2 || raw === '2') return 'warm';
    if (raw === 1 || raw === '1') return 'partial';
    if (raw === 0 || raw === '0') return 'cold';
    const text = String(raw ?? 'cold').toLowerCase();
    if (text === 'warm' || text === 'partial' || text === 'cold') return text;
    return 'cold';
}

function normalizeLane(lane, idx, sessionIds) {
    if (Array.isArray(lane)) {
        const idIdx = lane[0];
        const resolvedId = Array.isArray(sessionIds) && typeof idIdx === 'number' && sessionIds[idIdx] !== undefined
            ? sessionIds[idIdx]
            : idIdx;
        return {
            id: String(resolvedId ?? idx),
            start_s: Number(lane[1] ?? 0),
            end_s: Number(lane[2] ?? lane[1] ?? 0),
            cache_cls: normalizeCacheClass(lane[3]),
            ttft_ms: lane[4] != null ? Number(lane[4]) : null,
        };
    }
    return {
        id: String(lane.chat_id ?? lane.session_id ?? lane.id ?? idx),
        start_s: Number(lane.start_s ?? lane.start_seconds ?? 0),
        end_s: Number(lane.end_s ?? lane.end_seconds ?? lane.start_s ?? 0),
        cache_cls: normalizeCacheClass(lane.cache_bucket ?? lane.cache_cls),
        ttft_ms: lane.ttft_ms != null ? Number(lane.ttft_ms) : null,
    };
}

const SLOT_TIER_META = {
    cold: {
        label: 'cold (<10%)',
        color: 'var(--color-rose-500, rgb(248, 81, 73))',
        swatchClass: 'bg-rose-500',
    },
    partial: {
        label: 'partial (10–70%)',
        color: 'var(--color-amber-500, rgb(245, 158, 11))',
        swatchClass: 'bg-amber-500',
    },
    warm: {
        label: 'warm (≥70%)',
        color: 'var(--color-emerald-500, rgb(16, 185, 129))',
        swatchClass: 'bg-emerald-500',
    },
};

function classifyCacheTier(cacheHitPct, rawTier) {
    if (cacheHitPct !== null && cacheHitPct !== undefined && Number.isFinite(Number(cacheHitPct))) {
        const pct = Number(cacheHitPct);
        if (pct >= 70) return 'warm';
        if (pct >= 10) return 'partial';
        return 'cold';
    }
    return normalizeCacheClass(rawTier);
}

function defaultCachePctForTier(tier) {
    if (tier === 'warm') return 100;
    if (tier === 'partial') return 40;
    return 0;
}

function normalizeSlotLanesData(clientSeries, sessions) {
    const slotLanes = clientSeries?.slot_lanes;
    if (slotLanes && Array.isArray(slotLanes.items) && slotLanes.items.length > 0) {
        const items = slotLanes.items.map((item) => {
            const startS = Math.max(0, firstFinite(item.start_s, item.start_seconds, 0) ?? 0);
            const endS = Math.max(startS, firstFinite(item.end_s, item.end_seconds, startS) ?? startS);
            const rawPct = firstFinite(item.cache_hit_pct, item.prefix_hit_pct, item.cache_hit_percent);
            const tier = classifyCacheTier(rawPct, item.tier ?? item.cache_bucket ?? item.cache_cls);
            const cacheHitPct = rawPct !== null
                ? Math.min(100, Math.max(0, rawPct))
                : defaultCachePctForTier(tier);
            const slot = Math.max(0, Math.trunc(firstFinite(item.slot, 0) ?? 0));
            return {
                slot,
                start_s: startS,
                end_s: endS,
                cache_hit_pct: cacheHitPct,
                tier,
                session_id: item.session_id != null ? String(item.session_id) : '',
                turn: firstFinite(item.turn),
                input_tokens: firstFinite(item.input_tokens),
                output_tokens: firstFinite(item.output_tokens),
                ttft_ms: firstFinite(item.ttft_ms),
            };
        });
        const maxSlot = items.reduce((acc, it) => Math.max(acc, it.slot), 0);
        const peakSlots = Math.max(maxSlot + 1, Math.trunc(firstFinite(slotLanes.peak_slots, 0) ?? 0));
        const totalRequests = Math.trunc(firstFinite(slotLanes.total_requests, items.length) ?? items.length);
        const uniqueSessions = new Set(items.map((it) => it.session_id).filter(Boolean));
        const sessionCount = Math.trunc(firstFinite(slotLanes.session_count, uniqueSessions.size) ?? uniqueSessions.size);
        const sampleStride = Math.max(1, Math.trunc(firstFinite(slotLanes.sample_stride, 1) ?? 1));
        const tierCounts = slotLanes.tier_counts || items.reduce(
            (acc, it) => ({ ...acc, [it.tier]: (acc[it.tier] || 0) + 1 }),
            { cold: 0, partial: 0, warm: 0 },
        );
        return {
            peak_slots: peakSlots,
            total_requests: totalRequests,
            session_count: sessionCount,
            sample_stride: sampleStride,
            tier_counts: tierCounts,
            items,
        };
    }

    if (sessions?.available === false) return null;

    // Client-side greedy slot-packing fallback for legacy telemetry without clientSeries.slot_lanes
    const rawEntries = [];
    if (Array.isArray(sessions?.sessions) && sessions.sessions.length > 0) {
        sessions.sessions.forEach((sess, sIdx) => {
            const sid = String(sess.session_id ?? sess.chat_id ?? sess.id ?? sIdx);
            const turns = Array.isArray(sess.turns)
                ? sess.turns
                : (Array.isArray(sess.requests) ? sess.requests : null);
            if (turns && turns.length > 0) {
                turns.forEach((req, tIdx) => {
                    const startS = firstFinite(req.start_s, req.start_seconds, req.arrived_rel);
                    const endS = firstFinite(req.end_s, req.end_seconds, req.completed_rel, startS);
                    if (startS === null || endS === null) return;
                    const clampedStart = Math.max(0, startS);
                    const clampedEnd = Math.max(clampedStart, endS);
                    const rawPct = firstFinite(req.cache_hit_pct, req.prefix_hit_pct, req.cache_hit_percent);
                    const tier = classifyCacheTier(rawPct, req.tier ?? req.cache_bucket ?? req.cache_cls);
                    rawEntries.push({
                        start_s: clampedStart,
                        end_s: clampedEnd,
                        cache_hit_pct: rawPct !== null ? Math.min(100, Math.max(0, rawPct)) : defaultCachePctForTier(tier),
                        tier,
                        session_id: sid,
                        turn: firstFinite(req.turn, tIdx + 1),
                        input_tokens: firstFinite(req.input_tokens),
                        output_tokens: firstFinite(req.output_tokens),
                        ttft_ms: firstFinite(req.ttft_ms),
                    });
                });
            } else {
                const startS = firstFinite(sess.start_s, sess.start_seconds);
                const endS = firstFinite(sess.end_s, sess.end_seconds, startS);
                if (startS !== null && endS !== null) {
                    const clampedStart = Math.max(0, startS);
                    const clampedEnd = Math.max(clampedStart, endS);
                    const rawPct = firstFinite(sess.cache_hit_pct, sess.prefix_hit_pct, sess.cache_hit_percent);
                    const tier = classifyCacheTier(rawPct, sess.tier ?? sess.cache_bucket ?? sess.cache_cls);
                    rawEntries.push({
                        start_s: clampedStart,
                        end_s: clampedEnd,
                        cache_hit_pct: rawPct !== null ? Math.min(100, Math.max(0, rawPct)) : defaultCachePctForTier(tier),
                        tier,
                        session_id: sid,
                        turn: firstFinite(sess.turn),
                        input_tokens: firstFinite(sess.input_tokens),
                        output_tokens: firstFinite(sess.output_tokens),
                        ttft_ms: firstFinite(sess.ttft_ms),
                    });
                }
            }
        });
    } else if (Array.isArray(sessions?.lanes) && sessions.lanes.length > 0) {
        sessions.lanes.forEach((lane, idx) => {
            const normalized = normalizeLane(lane, idx, sessions.session_ids);
            const startS = Math.max(0, Number.isFinite(normalized.start_s) ? normalized.start_s : 0);
            const endS = Math.max(startS, Number.isFinite(normalized.end_s) ? normalized.end_s : startS);
            const tier = normalizeCacheClass(normalized.cache_cls);
            rawEntries.push({
                start_s: startS,
                end_s: endS,
                cache_hit_pct: defaultCachePctForTier(tier),
                tier,
                session_id: normalized.id,
                turn: null,
                input_tokens: null,
                output_tokens: null,
                ttft_ms: normalized.ttft_ms,
            });
        });
    }

    if (!rawEntries.length) return null;

    rawEntries.sort((a, b) => (a.start_s - b.start_s) || (a.end_s - b.end_s));
    const slotFreeAt = [];
    const tierCounts = { cold: 0, partial: 0, warm: 0 };
    const packed = rawEntries.map((entry) => {
        let slot = slotFreeAt.findIndex((freeAt) => freeAt <= entry.start_s);
        if (slot === -1) {
            slot = slotFreeAt.length;
            slotFreeAt.push(entry.end_s);
        } else {
            slotFreeAt[slot] = entry.end_s;
        }
        tierCounts[entry.tier] = (tierCounts[entry.tier] || 0) + 1;
        return { ...entry, slot };
    });

    const sampleStride = packed.length > 2000 ? Math.ceil(packed.length / 2000) : 1;
    const items = sampleStride > 1 ? packed.filter((_, idx) => idx % sampleStride === 0) : packed;
    const sessionCount = new Set(packed.map((it) => it.session_id).filter(Boolean)).size;

    return {
        peak_slots: slotFreeAt.length,
        total_requests: packed.length,
        session_count: sessionCount,
        sample_stride: sampleStride,
        tier_counts: tierCounts,
        items,
    };
}

function normalizeHeatmapData(raw, sequenceLength) {
    if (!raw || typeof raw !== 'object') return null;

    const rawPoints = Array.isArray(raw.points) ? raw.points : [];
    const points = rawPoints
        .map((pt) => {
            if (!pt || typeof pt !== 'object') return null;
            const t = firstFinite(pt.t, pt.elapsed_seconds, pt.time_seconds);
            const seq = firstFinite(pt.seq, pt.sequence_length);
            const ms = firstFinite(pt.ms, pt.value, pt.metric_ms);
            if (t === null || seq === null || ms === null) return null;
            if (t < 0 || seq < 0 || ms < 0) return null;
            return { t, seq, ms };
        })
        .filter(Boolean);

    const rawCells = Array.isArray(raw.cells) ? raw.cells : [];
    const cells = rawCells.map((cell) => {
        const p50 = firstFinite(cell.p50_ms, cell.p50);
        const p90 = firstFinite(cell.p90_ms, cell.p90);
        const maxMs = firstFinite(cell.max_ms, cell.max);
        return {
            start_seconds: firstFinite(cell.start_seconds) ?? 0,
            end_seconds: firstFinite(cell.end_seconds) ?? 0,
            sequence_length_start: firstFinite(cell.sequence_length_start, cell.seq_start) ?? 0,
            sequence_length_end: firstFinite(cell.sequence_length_end, cell.seq_end) ?? 0,
            average_metric_ms: firstFinite(cell.average_metric_ms, cell.avg_ms) ?? 0,
            ...(p50 !== null ? { p50_ms: p50 } : {}),
            ...(p90 !== null ? { p90_ms: p90 } : {}),
            ...(maxMs !== null ? { max_ms: maxMs } : {}),
            request_count: firstFinite(cell.request_count, cell.count) ?? 0,
        };
    });

    if (!points.length && !cells.length) return null;

    const timeBinCount = firstFinite(raw.time_bin_count, raw.time_bins);
    const seqBinCount = firstFinite(raw.sequence_length_bin_count, raw.len_bins);
    const durationSeconds = firstFinite(raw.duration_seconds);
    const windowStartSeconds = firstFinite(raw.window_start_seconds);
    const rawTotal = firstFinite(raw.points_total);
    const rawSampled = firstFinite(raw.points_sampled);
    const pointsTotal = points.length > 0
        ? Math.max(points.length, rawTotal !== null ? Math.trunc(rawTotal) : 0)
        : (rawTotal !== null && rawTotal >= 0 ? Math.trunc(rawTotal) : 0);
    const pointsSampled = points.length > 0
        ? points.length
        : (rawSampled !== null && rawSampled >= 0 ? Math.trunc(rawSampled) : 0);

    const metricValues = points.length > 0
        ? points.map((pt) => pt.ms)
        : cells.map((c) => c.average_metric_ms).filter(Number.isFinite);
    const fallbackMin = metricValues.length ? Math.min(...metricValues) : 0;
    const fallbackMax = metricValues.length ? Math.max(...metricValues) : 0;
    const rawMin = firstFinite(raw.minimum_metric_ms, raw.min_ms);
    const rawMax = firstFinite(raw.maximum_metric_ms, raw.max_ms);
    const hasValidExplicitRange = rawMin !== null && rawMax !== null && rawMin <= rawMax;
    const minMetric = hasValidExplicitRange
        ? (points.length > 0 ? Math.min(rawMin, fallbackMin) : rawMin)
        : fallbackMin;
    const maxMetric = hasValidExplicitRange
        ? (points.length > 0 ? Math.max(rawMax, fallbackMax) : rawMax)
        : fallbackMax;

    return {
        ...raw,
        points,
        points_total: pointsTotal,
        points_sampled: pointsSampled,
        ...(durationSeconds !== null && durationSeconds >= 0 ? { duration_seconds: durationSeconds } : {}),
        ...(windowStartSeconds !== null && windowStartSeconds >= 0 ? { window_start_seconds: windowStartSeconds } : {}),
        sequence_length: raw.sequence_length || sequenceLength,
        ...(raw.sequence_length_source ? { sequence_length_source: raw.sequence_length_source } : {}),
        ...(timeBinCount !== null ? { time_bin_count: timeBinCount } : {}),
        ...(seqBinCount !== null ? { sequence_length_bin_count: seqBinCount } : {}),
        min_ms: minMetric,
        max_ms: maxMetric,
        minimum_metric_ms: minMetric,
        maximum_metric_ms: maxMetric,
        cells,
    };
}

const SWIMLANE_TARGET_PLOT_HEIGHT = 200;
const SWIMLANE_MIN_ROW_HEIGHT = 8;
const SWIMLANE_MAX_VIEWPORT_HEIGHT = 360;
const SWIMLANE_FALLBACK_WIDTH = 960;
const SWIMLANE_MIN_WIDTH = 480;

function getTrimmedSessionId(item) {
    const raw = item?.session_id;
    return raw == null ? '' : String(raw).trim();
}

function SlotSwimlaneChart({ slotData, warmupSeconds = 0 }) {
    const containerRef = useRef(null);
    const [hovered, setHovered] = useState(null);
    const [selectedSessionId, setSelectedSessionId] = useState(null);
    const [measuredWidth, setMeasuredWidth] = useState(null);

    useEffect(() => {
        const el = containerRef.current;
        if (!el) return undefined;
        const update = (entries) => {
            const nextWidth = Math.round(entries?.[0]?.contentRect?.width || el.clientWidth || 0);
            if (nextWidth > 0) {
                setMeasuredWidth((prev) => (prev === nextWidth ? prev : nextWidth));
            }
        };
        update();
        if (typeof ResizeObserver === 'undefined') return undefined;
        const observer = new ResizeObserver(update);
        observer.observe(el);
        return () => observer.disconnect();
    }, []);

    const items = useMemo(() => slotData?.items || [], [slotData?.items]);
    const sessionTurnCounts = useMemo(() => {
        const counts = new Map();
        for (const item of items) {
            const sid = getTrimmedSessionId(item);
            if (!sid) continue;
            counts.set(sid, (counts.get(sid) || 0) + 1);
        }
        return counts;
    }, [items]);

    const activeSessionId = selectedSessionId && sessionTurnCounts.has(selectedSessionId)
        ? selectedSessionId
        : null;

    const activeSessionTurns = useMemo(() => {
        if (!activeSessionId) return [];
        return items
            .filter((item) => getTrimmedSessionId(item) === activeSessionId)
            .sort((a, b) => (
                (a.start_s - b.start_s)
                || (a.end_s - b.end_s)
                || ((a.turn ?? 0) - (b.turn ?? 0))
            ));
    }, [activeSessionId, items]);

    const peakSlots = Math.max(1, Number(slotData?.peak_slots || 1));
    const maxTime = useMemo(
        () => items.reduce((acc, item) => Math.max(acc, item.end_s), Math.max(1, warmupSeconds)),
        [items, warmupSeconds],
    );

    const plotHeight = Math.max(SWIMLANE_TARGET_PLOT_HEIGHT, peakSlots * SWIMLANE_MIN_ROW_HEIGHT);
    const rowH = plotHeight / peakSlots;
    const rowGap = rowH >= 10 ? 3 : rowH >= 7 ? 2 : 1;
    const barH = Math.min(18, Math.max(2, rowH - rowGap));

    const svgWidth = Math.max(SWIMLANE_MIN_WIDTH, Math.round(measuredWidth || SWIMLANE_FALLBACK_WIDTH));
    const padLeft = 44;
    const padRight = 16;
    const padTop = warmupSeconds > 0 ? 18 : 10;
    const padBottom = 24;
    const plotWidth = svgWidth - padLeft - padRight;
    const svgHeight = padTop + plotHeight + padBottom;
    const needsScroll = svgHeight > SWIMLANE_MAX_VIEWPORT_HEIGHT;

    const yTickIndices = useMemo(() => {
        const maxReadableTicks = Math.min(20, Math.max(2, Math.floor(plotHeight / 14)));
        const step = peakSlots <= maxReadableTicks ? 1 : Math.ceil(peakSlots / maxReadableTicks);
        const indices = [];
        for (let s = 0; s < peakSlots; s += step) {
            indices.push(s);
        }
        const lastSlot = peakSlots - 1;
        if (lastSlot > 0 && indices[indices.length - 1] !== lastSlot) {
            if (lastSlot - indices[indices.length - 1] < Math.max(2, Math.floor(step / 2))) {
                indices[indices.length - 1] = lastSlot;
            } else {
                indices.push(lastSlot);
            }
        }
        return indices;
    }, [peakSlots, plotHeight]);

    const xTicks = useMemo(
        () => [0, 0.2, 0.4, 0.6, 0.8, 1].map((frac) => frac * maxTime),
        [maxTime],
    );

    const handleBarHover = (item, event) => {
        const rect = containerRef.current?.getBoundingClientRect();
        if (!rect) {
            setHovered({ item, xPx: 24, yPx: 24, containerW: 600, containerH: 320 });
            return;
        }
        setHovered({
            item,
            xPx: event.clientX - rect.left,
            yPx: event.clientY - rect.top,
            containerW: rect.width,
            containerH: rect.height,
        });
    };

    const handleClearSelection = () => {
        setSelectedSessionId(null);
    };

    const handleBarClick = (item, event) => {
        event?.stopPropagation?.();
        const sid = getTrimmedSessionId(item);
        if (!sid) {
            setSelectedSessionId(null);
            return;
        }
        setSelectedSessionId((prev) => (prev === sid ? null : sid));
    };

    const handleChartKeyDown = (event) => {
        if (event?.key === 'Escape') {
            if (activeSessionId) event.preventDefault?.();
            setSelectedSessionId(null);
        }
    };

    const getBarGeometry = (item) => {
        const x = padLeft + (item.start_s / maxTime) * plotWidth;
        const w = Math.max(2.5, ((item.end_s - item.start_s) / maxTime) * plotWidth);
        const y = padTop + item.slot * rowH + (rowH - barH) / 2;
        return { x, y, w, midY: y + barH / 2 };
    };

    const renderBarRect = (item, key, isSelectedTurn) => {
        const { x, y, w } = getBarGeometry(item);
        const sid = getTrimmedSessionId(item);
        const isHovered = hovered?.item === item;
        const fillColor = SLOT_TIER_META[item.tier]?.color || SLOT_TIER_META.cold.color;
        const barWidth = isSelectedTurn ? Math.max(4, w) : w;
        const fillOpacity = isSelectedTurn
            ? 1
            : (activeSessionId ? (isHovered ? 0.45 : 0.16) : (isHovered ? 1 : 0.9));
        const stroke = isSelectedTurn ? 'var(--bg-card)' : (isHovered ? 'var(--text-primary)' : 'none');
        const strokeWidth = isSelectedTurn ? 0.75 : (isHovered ? 1.25 : 0);

        return (
            <rect
                key={key}
                x={x}
                y={y}
                width={barWidth}
                height={barH}
                rx={Math.min(3, barH / 2)}
                fill={fillColor}
                fillOpacity={fillOpacity}
                stroke={stroke}
                strokeWidth={strokeWidth}
                className="cursor-pointer"
                data-session-id={sid || undefined}
                role={sid ? 'button' : undefined}
                aria-pressed={sid ? activeSessionId === sid : undefined}
                aria-label={sid
                    ? `Session ${sid}${item.turn != null ? `, Turn ${item.turn}` : ''}, Slot S${item.slot + 1}`
                    : undefined}
                onMouseEnter={(event) => handleBarHover(item, event)}
                onMouseMove={(event) => handleBarHover(item, event)}
                onMouseLeave={() => setHovered(null)}
                onClick={(event) => handleBarClick(item, event)}
            />
        );
    };

    const warmupW = warmupSeconds > 0
        ? Math.min(plotWidth, (warmupSeconds / maxTime) * plotWidth)
        : 0;
    const warmupLineX = padLeft + warmupW;
    // Place 'Warmup' on the LEFT of warmupLineX (textAnchor="end"), clamped only so it never clips past x = 0.
    const warmupLabelX = Math.max(padLeft, warmupLineX - 4);
    const warmupLabelAnchor = 'end';

    const hoveredSid = hovered ? getTrimmedSessionId(hovered.item) : '';
    const hoveredTurnCount = hoveredSid ? (sessionTurnCounts.get(hoveredSid) || 0) : 0;
    const isHoveredSelected = Boolean(hoveredSid && activeSessionId === hoveredSid);
    const hoveredSessionText = hovered
        ? (hoveredSid
            ? (hovered.item.turn != null
                ? (hoveredTurnCount > 1
                    ? `${hoveredSid} · Turn ${hovered.item.turn} of ${hoveredTurnCount}`
                    : `${hoveredSid} · Turn ${hovered.item.turn}`)
                : (hoveredTurnCount > 1
                    ? `${hoveredSid} · ${hoveredTurnCount} turns`
                    : hoveredSid))
            : (hovered.item.turn != null ? `Turn ${hovered.item.turn}` : '—'))
        : '';

    const activeTurnCount = activeSessionTurns.length;

    const svgElement = (
        <svg
            viewBox={`0 0 ${svgWidth} ${svgHeight}`}
            width="100%"
            height={svgHeight}
            className="block min-w-[480px] w-full select-none"
            role="img"
            aria-label={`Sessions over time concurrent slot swimlane with ${formatCount(peakSlots)} slots at peak and ${formatCount(slotData.total_requests)} requests`}
            onMouseLeave={() => setHovered(null)}
            onClick={handleClearSelection}
            onKeyDown={handleChartKeyDown}
        >
            <rect
                x={padLeft}
                y={padTop}
                width={plotWidth}
                height={plotHeight}
                rx={4}
                fill="var(--bg-card)"
                fillOpacity={0.45}
                stroke="var(--border-color)"
                strokeOpacity={0.45}
                strokeWidth={1}
                onClick={handleClearSelection}
            />
            {Array.from({ length: peakSlots }, (_, slotIdx) => (
                <g key={`slot-row-${slotIdx}`} pointerEvents="none">
                    {slotIdx % 2 === 1 && (
                        <rect
                            x={padLeft}
                            y={padTop + slotIdx * rowH}
                            width={plotWidth}
                            height={rowH}
                            fill="var(--text-muted)"
                            fillOpacity={0.04}
                        />
                    )}
                    {slotIdx < peakSlots - 1 && (
                        <line
                            x1={padLeft}
                            y1={padTop + (slotIdx + 1) * rowH}
                            x2={padLeft + plotWidth}
                            y2={padTop + (slotIdx + 1) * rowH}
                            stroke="var(--border-color)"
                            strokeOpacity={0.25}
                            strokeWidth={0.5}
                        />
                    )}
                </g>
            ))}
            {xTicks.map((tick, idx) => {
                const tx = padLeft + (tick / maxTime) * plotWidth;
                return (
                    <g key={`xtick-${idx}`} pointerEvents="none">
                        <line
                            x1={tx}
                            y1={padTop}
                            x2={tx}
                            y2={padTop + plotHeight}
                            stroke="var(--border-color)"
                            strokeDasharray="3 3"
                            strokeOpacity={0.45}
                            strokeWidth={0.75}
                        />
                        <text
                            x={tx}
                            y={padTop + plotHeight + 16}
                            textAnchor={idx === 0 ? 'start' : idx === xTicks.length - 1 ? 'end' : 'middle'}
                            fill="var(--text-muted)"
                            fontSize="11"
                        >
                            {`${formatNumber(tick)}s`}
                        </text>
                    </g>
                );
            })}
            {warmupW > 0 && (
                <g pointerEvents="none">
                    <rect
                        x={padLeft}
                        y={padTop}
                        width={warmupW}
                        height={plotHeight}
                        fill="var(--text-muted)"
                        fillOpacity={0.14}
                    />
                    <line
                        x1={padLeft + warmupW}
                        y1={padTop}
                        x2={padLeft + warmupW}
                        y2={padTop + plotHeight}
                        stroke="var(--text-muted)"
                        strokeDasharray="4 3"
                        strokeOpacity={0.6}
                        strokeWidth={1}
                    />
                </g>
            )}
            {yTickIndices.map((slotIdx) => (
                <text
                    key={`ylabel-${slotIdx}`}
                    x={padLeft - 6}
                    y={padTop + slotIdx * rowH + rowH / 2 + 3.5}
                    textAnchor="end"
                    fill="var(--text-muted)"
                    fontSize="10"
                    fontFamily="monospace"
                    pointerEvents="none"
                >
                    {`S${slotIdx + 1}`}
                </text>
            ))}
            {(activeSessionId
                ? items.filter((item) => getTrimmedSessionId(item) !== activeSessionId)
                : items
            ).map((item, idx) => renderBarRect(item, `${item.slot}-${item.start_s}-${item.end_s}-${idx}`, false))}
            {activeSessionId && activeSessionTurns.length >= 2 && (
                <g data-session-connectors={activeSessionId} pointerEvents="none">
                    {activeSessionTurns.slice(0, -1).map((turnItem, idx) => {
                        const nextItem = activeSessionTurns[idx + 1];
                        const currGeom = getBarGeometry(turnItem);
                        const nextGeom = getBarGeometry(nextItem);
                        return (
                            <line
                                key={`connector-${idx}-${turnItem.start_s}-${nextItem.start_s}`}
                                x1={currGeom.x + Math.max(4, currGeom.w)}
                                y1={currGeom.midY}
                                x2={nextGeom.x}
                                y2={nextGeom.midY}
                                stroke="var(--text-primary)"
                                strokeOpacity={0.55}
                                strokeWidth={1.25}
                                strokeDasharray="3 3"
                            />
                        );
                    })}
                </g>
            )}
            {activeSessionId && (
                <g data-selected-session={activeSessionId}>
                    {activeSessionTurns.map((item, idx) => {
                        const { x, y, w } = getBarGeometry(item);
                        const highlightW = Math.max(4, w);
                        return (
                            <g key={`selected-${item.slot}-${item.start_s}-${item.end_s}-${idx}`}>
                                {renderBarRect(item, `selected-bar-${idx}`, true)}
                                <rect
                                    x={x - 1.5}
                                    y={y - 1.5}
                                    width={highlightW + 3}
                                    height={barH + 3}
                                    rx={Math.min(4, (barH + 3) / 2)}
                                    fill="none"
                                    stroke="var(--text-primary)"
                                    strokeOpacity={0.95}
                                    strokeWidth={1.75}
                                    pointerEvents="none"
                                />
                            </g>
                        );
                    })}
                </g>
            )}
            {warmupW > 0 && (
                <text
                    x={warmupLabelX}
                    y={padTop - 4}
                    textAnchor={warmupLabelAnchor}
                    fill="var(--text-muted)"
                    fontSize={10}
                    pointerEvents="none"
                >
                    Warmup
                </text>
            )}
        </svg>
    );

    return (
        <div className="space-y-3">
            <div
                ref={containerRef}
                tabIndex={0}
                onKeyDown={handleChartKeyDown}
                className="relative rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/40"
            >
                <div
                    className={cn(needsScroll ? 'max-h-[360px] overflow-auto' : 'overflow-x-auto')}
                    onScroll={() => setHovered(null)}
                >
                    {svgElement}
                </div>
                {hovered && (
                    <div
                        className="pointer-events-none absolute z-30"
                        style={{
                            left: `${Math.min(Math.max(8, hovered.xPx + 12), Math.max(8, (hovered.containerW || 600) - 240))}px`,
                            top: `${Math.max(8, Math.min(hovered.yPx - 16, Math.max(8, (hovered.containerH || 320) - 170)))}px`,
                        }}
                    >
                        <ChartTooltip title={`Slot S${hovered.item.slot + 1}`}>
                            <ChartTooltipRow
                                label="Session"
                                value={hoveredSessionText}
                            />
                            <ChartTooltipRow
                                label="Start – End"
                                value={`${formatNumber(hovered.item.start_s)}–${formatNumber(hovered.item.end_s)}s (${formatNumber(Math.max(0, hovered.item.end_s - hovered.item.start_s))}s)`}
                            />
                            <ChartTooltipRow
                                color={SLOT_TIER_META[hovered.item.tier]?.color}
                                label="Prefix Cache Hit"
                                value={(() => {
                                    const hitText = formatDecimal(hovered.item.cache_hit_pct);
                                    return hitText === '—' ? '—' : `${hitText}% (${hovered.item.tier})`;
                                })()}
                            />
                            <ChartTooltipRow
                                label="Prompt / Output"
                                value={`${hovered.item.input_tokens != null ? formatCount(hovered.item.input_tokens) : '—'} / ${hovered.item.output_tokens != null ? formatCount(hovered.item.output_tokens) : '—'} tok`}
                            />
                            <ChartTooltipRow
                                label="TTFT"
                                value={hovered.item.ttft_ms != null ? formatMilliseconds(hovered.item.ttft_ms) : '—'}
                            />
                            {hoveredTurnCount > 1 && (
                                <div className="mt-1.5 border-t border-theme-border pt-1 text-[10px] text-theme-muted">
                                    {isHoveredSelected
                                        ? 'Click again to clear highlight'
                                        : `Click to highlight all ${hoveredTurnCount} turns`}
                                </div>
                            )}
                        </ChartTooltip>
                    </div>
                )}
            </div>
            {activeSessionId && (
                <div
                    data-testid="selected-session-pill"
                    className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-theme-border bg-theme-card px-2.5 py-1.5 text-[11px] text-theme-text"
                >
                    <span className="font-medium">
                        <span className="text-theme-muted">Selected session: </span>
                        <span className="font-mono font-semibold text-theme-text">{activeSessionId}</span>
                        <span className="text-theme-muted">
                            {` · ${formatCount(activeTurnCount)} ${activeTurnCount === 1 ? 'turn' : 'turns'}`}
                        </span>
                    </span>
                    <Button
                        type="button"
                        variant="ghost"
                        size="xs"
                        onClick={handleClearSelection}
                        aria-label={`Clear selected session ${activeSessionId}`}
                    >
                        Clear
                    </Button>
                </div>
            )}
            <div className="flex flex-wrap items-center justify-between gap-3 text-[11px] text-theme-muted">
                <div className="flex flex-wrap items-center gap-4">
                    {(['cold', 'partial', 'warm']).map((tierKey) => (
                        <span key={tierKey} className="inline-flex items-center gap-1.5 font-medium">
                            <span
                                className={cn('h-3 w-3 shrink-0 rounded-sm', SLOT_TIER_META[tierKey].swatchClass)}
                                aria-hidden="true"
                            />
                            <span>{SLOT_TIER_META[tierKey].label}</span>
                        </span>
                    ))}
                    {warmupSeconds > 0 && (
                        <span className="inline-flex items-center gap-1.5 font-medium">
                            <span
                                className="h-3 w-3 shrink-0 rounded-sm border border-theme-border bg-theme-muted/20"
                                aria-hidden="true"
                            />
                            <span>warmup</span>
                        </span>
                    )}
                    <HelpTooltip
                        label="Session cache classification help"
                        align="start"
                        iconSize={11}
                        formula="Slot_Tier: Cold < 10%, Partial 10–70%, Warm ≥ 70%  (Cache_Hit_% = Matched_Prefix_Tokens / Prompt_Tokens × 100)"
                    >
                        Each request is colored by prompt prefix-cache hit percentage: cold (&lt;10%), partial (10–70%), or warm (≥70%).
                    </HelpTooltip>
                </div>
                <span className="font-mono">
                    {`${formatCount(slotData.peak_slots)} slots at peak · ${formatCount(slotData.total_requests)} requests · ${formatCount(slotData.session_count)} sessions`}
                    {slotData.sample_stride > 1 ? ` · 1 in ${formatCount(slotData.sample_stride)} sampled` : ''}
                </span>
            </div>
        </div>
    );
}

function sparseDotProp(data, dataKey, color) {
    if (!Array.isArray(data) || !data.length) return false;
    const isFiniteAt = (idx) => {
        if (idx < 0 || idx >= data.length) return false;
        const val = data[idx]?.[dataKey];
        return val !== null && val !== undefined && val !== '' && Number.isFinite(Number(val));
    };
    const nonNullIndices = [];
    for (let i = 0; i < data.length; i += 1) {
        if (isFiniteAt(i)) nonNullIndices.push(i);
    }
    if (nonNullIndices.length === 0) return false;
    if (nonNullIndices.length === 1) {
        return { r: 3.5, fill: color, stroke: 'var(--bg-card)', strokeWidth: 1.5 };
    }
    const isolatedPayloads = new Set(
        nonNullIndices
            .filter((idx) => !isFiniteAt(idx - 1) && !isFiniteAt(idx + 1))
            .map((idx) => data[idx]),
    );
    if (isolatedPayloads.size === 0) return false;
    return function IsolatedDot(dotProps) {
        const { cx, cy, value, payload, index } = dotProps || {};
        if (value === null || value === undefined || !Number.isFinite(cx) || !Number.isFinite(cy)) return null;
        if (!isolatedPayloads.has(payload)) return null;
        return (
            <circle
                key={`${dataKey}-dot-${index}`}
                className="recharts-dot recharts-line-dot"
                cx={cx}
                cy={cy}
                r={3.5}
                fill={color}
                stroke="var(--bg-card)"
                strokeWidth={1.5}
            />
        );
    };
}

function buildTooltip(unit) {
    return function RequestsTooltip({ active, payload, label }) {
        if (!active || !payload?.length) return null;
        const rows = payload.filter((item) => item.value !== null && item.value !== undefined);
        if (!rows.length) return null;
        const xUnit = unit === '%-tpot' ? 'ms/tok' : unit === '%' ? 'ms' : 's';
        const rowUnit = unit.startsWith('%') ? '%' : unit;
        return (
            <ChartTooltip title={typeof label === 'number' ? `${formatNumber(label)} ${xUnit}` : String(label)}>
                {rows.map((item) => (
                    <ChartTooltipRow
                        key={item.dataKey}
                        color={item.color}
                        label={item.name}
                        value={rowUnit === 'ms' ? formatMilliseconds(item.value) : formatNumber(item.value)}
                        unit={rowUnit === 'ms' ? '' : ` ${rowUnit}`}
                    />
                ))}
            </ChartTooltip>
        );
    };
}

const RpsTooltip = buildTooltip('req/s');
const MsTooltip = buildTooltip('ms');
const CountTooltip = buildTooltip('req');
const SessionsTooltip = buildTooltip('sessions');
const CdfTooltip = buildTooltip('%');
const TpotCdfTooltip = buildTooltip('%-tpot');

function formatTurnLabel(raw) {
    const text = String(raw ?? '').trim();
    if (!text) return '';
    if (/\bturns?\b/i.test(text)) return text;
    const numeric = Number(text);
    if (Number.isFinite(numeric)) {
        return numeric === 1 ? '1 turn' : `${formatNumber(numeric)} turns`;
    }
    return `${text} turns`;
}

function normalizeBuckets(raw, preferredOrder) {
    if (!raw) return [];
    const items = Array.isArray(raw)
        ? raw
        : Object.entries(raw).map(([bucket, stats]) => ({ bucket, ...stats }));
    const mapped = items.map((item) => ({
        bucket: item.bucket || item.label || item.name || '',
        p50: firstFinite(item.p50_ms, item.p50, item.ttft_p50_ms),
        p90: firstFinite(item.p90_ms, item.p90, item.ttft_p90_ms),
        count: firstFinite(item.count, item.requests),
    })).filter((item) => item.bucket && (item.p50 !== null || item.p90 !== null));
    if (!preferredOrder) return mapped;
    return mapped.sort((a, b) => {
        const ai = preferredOrder.indexOf(a.bucket);
        const bi = preferredOrder.indexOf(b.bucket);
        if (ai !== -1 && bi !== -1) return ai - bi;
        if (ai !== -1) return -1;
        if (bi !== -1) return 1;
        return a.bucket.localeCompare(b.bucket);
    });
}

function normalizeDistHistogram(raw) {
    if (!raw) return [];
    if (Array.isArray(raw)) {
        return raw.map((item, idx) => ({
            label: String(
                item.turns
                ?? item.depth
                ?? item.label
                ?? item.bucket
                ?? item.bin
                ?? (item.min_ms != null && item.max_ms != null ? `${formatDecimal(item.min_ms)}–${formatDecimal(item.max_ms)}ms` : idx),
            ),
            count: Number(item.sessions ?? item.count ?? item.requests ?? item.value ?? 0),
        }));
    }
    if (typeof raw === 'object') {
        return Object.entries(raw).map(([label, count]) => ({
            label,
            count: Number(count ?? 0),
        }));
    }
    return [];
}

function normalizePercentiles(source = {}) {
    return {
        avg: firstFinite(source.mean_ms, source.avg_ms, source.mean, source.avg),
        min: firstFinite(source.min_ms, source.min),
        p50: firstFinite(source.p50_ms, source.p50),
        p90: firstFinite(source.p90_ms, source.p90),
        p95: firstFinite(source.p95_ms, source.p95),
        p99: firstFinite(source.p99_ms, source.p99),
        max: firstFinite(source.max_ms, source.max),
    };
}

const EMPTY_OBJ = {};

export function RequestsTab({ task, telemetry, analysisWindow = 'profiling', onOpenErrorInspector }) {
    const [cdfMetric, setCdfMetric] = useState('ttft');

    const clientSeries = telemetry?.client_series ?? EMPTY_OBJ;
    const sessions = telemetry?.sessions_summary ?? EMPTY_OBJ;
    const windows = clientSeries.windows ?? telemetry?.forensics?.windows ?? EMPTY_OBJ;
    const activeWindowData = windows[analysisWindow] ?? windows.profiling ?? windows.full ?? clientSeries.summary ?? task?.result?.summary ?? EMPTY_OBJ;
    const summaryFallback = clientSeries.summary ?? task?.result?.summary ?? EMPTY_OBJ;
    const backendOptions = task?.simulation?.backend_options ?? task?.backend_options ?? EMPTY_OBJ;

    const rawBuckets = useMemo(() => {
        const list = Array.isArray(clientSeries.fidelity_series) && clientSeries.fidelity_series.length > 0
            ? clientSeries.fidelity_series
            : Array.isArray(clientSeries.buckets)
                ? clientSeries.buckets
                : Array.isArray(clientSeries.series)
                    ? clientSeries.series
                    : [];
        const defaultBucketSeconds = firstFinite(clientSeries.bucket_seconds);
        return list.map((b, idx) => {
            const dur = firstFinite(
                b.end_seconds != null && b.start_seconds != null ? Number(b.end_seconds) - Number(b.start_seconds) : null,
                defaultBucketSeconds,
            );
            const derivedArrivedRps = b.arrived_requests != null && dur && dur > 0
                ? Number(b.arrived_requests) / dur
                : null;
            return {
                t: firstFinite(b.mid_seconds, b.time_seconds, b.end_seconds, b.elapsed_seconds, b.t, idx),
                target_rps: firstFinite(b.target_arrival_rps, b.target_rps),
                actual_rps: firstFinite(
                    b.actual_send_rps,
                    b.dispatched_rps,
                    b.arrived_rps,
                    b.request_arrival_rps,
                    derivedArrivedRps,
                    b.completed_rps,
                    b.throughput_rps,
                    b.request_completion_rps,
                ),
                drift_p50: firstFinite(b.s_time_drift_p50_ms, b.drift_p50_ms, b.drift_ms?.p50, b.s_time_drift?.p50_ms),
                drift_p90: firstFinite(b.s_time_drift_p90_ms, b.drift_p90_ms, b.drift_ms?.p90, b.s_time_drift?.p90_ms),
                drift_p99: firstFinite(b.s_time_drift_p99_ms, b.drift_p99_ms, b.drift_ms?.p99, b.s_time_drift?.p99_ms),
            };
        });
    }, [clientSeries.bucket_seconds, clientSeries.buckets, clientSeries.fidelity_series, clientSeries.series]);

    const dispatchDurationSeconds = firstFinite(clientSeries.dispatch_duration_seconds);
    const lastLegacyBucketEnd = Array.isArray(clientSeries.buckets) && clientSeries.buckets.length > 0
        ? firstFinite(
            clientSeries.buckets[clientSeries.buckets.length - 1]?.end_seconds,
            clientSeries.buckets[clientSeries.buckets.length - 1]?.time_seconds,
        )
        : null;
    const totalDurationSeconds = firstFinite(
        clientSeries.duration_seconds,
        windows.full?.duration_seconds,
        summaryFallback.duration_seconds,
        lastLegacyBucketEnd,
    );
    const hasGraceDrain = dispatchDurationSeconds !== null
        && totalDurationSeconds !== null
        && (totalDurationSeconds - dispatchDurationSeconds) >= 3.0;
    const fidelityXDomain = dispatchDurationSeconds !== null && dispatchDurationSeconds > 0
        ? [0, dispatchDurationSeconds]
        : ['dataMin', 'dataMax'];
    const dispatchDurationText = formatDecimal(dispatchDurationSeconds);
    const totalDurationText = formatDecimal(totalDurationSeconds);
    const fidelitySubtitle = hasGraceDrain
        ? `Send window: 0–${dispatchDurationText}s · Grace drain: ${dispatchDurationText}–${totalDurationText}s (in-flight completion)`
        : 'Target trace arrival rate vs actual dispatched / completed rate';
    const driftSubtitle = hasGraceDrain
        ? `Client dispatch skew over send window (0–${dispatchDurationText}s · drain ${dispatchDurationText}–${totalDurationText}s)`
        : 'Client dispatch skew relative to trace schedule';

    const hasReplayRateSeries = rawBuckets.some((pt) => pt.target_rps !== null || pt.actual_rps !== null);
    const hasDriftSeries = rawBuckets.some((pt) => pt.drift_p50 !== null || pt.drift_p90 !== null);
    const driftHistogram = useMemo(
        () => normalizeDistHistogram(clientSeries.drift_histogram || clientSeries.s_time_drift_histogram),
        [clientSeries.drift_histogram, clientSeries.s_time_drift_histogram],
    );

    const driftStats = useMemo(() => normalizePercentiles(
        activeWindowData.drift_ms
        || activeWindowData.s_time_drift
        || activeWindowData.drift
        || summaryFallback.drift_ms
        || summaryFallback.s_time_drift
        || summaryFallback.drift
        || EMPTY_OBJ,
    ), [activeWindowData, summaryFallback]);

    // Sessions swimlane (slot-packed with client-side fallback for legacy payloads)
    const correlation = sessions.correlation ?? EMPTY_OBJ;
    const warmupSeconds = firstFinite(clientSeries.warmup_seconds, backendOptions.warmup_seconds, 0) || 0;
    const slotLanesData = useMemo(
        () => normalizeSlotLanesData(clientSeries, sessions),
        [clientSeries, sessions],
    );
    const slotSwimlaneSubtitle = warmupSeconds > 0
        ? 'One row per concurrent session slot · Requests packed into slots · a slot is reused once its last request finished · shaded span = warmup'
        : 'One row per concurrent session slot · Requests packed into slots · a slot is reused once its last request finished';

    // Per-request latency scatter (window-aware with legacy top-level fallback) & length / cache buckets
    const hasWindowHeatmap = Boolean(activeWindowData?.heatmap && typeof activeWindowData.heatmap === 'object');
    const rawTtftHeatmap = hasWindowHeatmap
        ? activeWindowData.heatmap.ttft
        : (clientSeries.heatmap?.ttft || clientSeries.ttft_heatmap || clientSeries.heatmaps?.ttft || summaryFallback.ttft_heatmap);
    const rawTpotHeatmap = hasWindowHeatmap
        ? activeWindowData.heatmap.tpot
        : (clientSeries.heatmap?.tpot || clientSeries.tpot_heatmap || clientSeries.heatmaps?.tpot || summaryFallback.tpot_heatmap);

    const ttftHeatmap = useMemo(
        () => normalizeHeatmapData(rawTtftHeatmap, 'input'),
        [rawTtftHeatmap],
    );
    const tpotHeatmap = useMemo(
        () => normalizeHeatmapData(rawTpotHeatmap, 'output'),
        [rawTpotHeatmap],
    );
    const promptLengthBuckets = useMemo(
        () => normalizeBuckets(clientSeries.prompt_length_buckets || clientSeries.prompt_buckets || activeWindowData.prompt_length_buckets, PROMPT_BUCKET_ORDER),
        [clientSeries.prompt_length_buckets, clientSeries.prompt_buckets, activeWindowData.prompt_length_buckets],
    );
    const cacheBuckets = useMemo(
        () => normalizeBuckets(sessions.cache_buckets || clientSeries.cache_hit_buckets || clientSeries.cache_buckets, CACHE_BUCKET_ORDER),
        [sessions.cache_buckets, clientSeries.cache_hit_buckets, clientSeries.cache_buckets],
    );

    // Conversation structure
    const turnsHistogram = useMemo(
        () => normalizeDistHistogram(sessions.turn_distribution || sessions.turns_histogram || sessions.turns_per_session)
            .map((entry) => ({ ...entry, label: formatTurnLabel(entry.label) })),
        [sessions.turn_distribution, sessions.turns_histogram, sessions.turns_per_session],
    );
    const depthHistogram = useMemo(
        () => normalizeDistHistogram(sessions.tree_depth_distribution || sessions.tree_depth_histogram || sessions.tree_depths),
        [sessions.tree_depth_distribution, sessions.tree_depth_histogram, sessions.tree_depths],
    );

    // CDF
    const hasWindowCdf = Boolean(activeWindowData?.cdf);
    const cdfMap = activeWindowData?.cdf ?? clientSeries.cdf ?? EMPTY_OBJ;
    const cdfPoints = useMemo(() => {
        const keyForMetric = cdfMetric === 'ttft'
            ? ['ttft', 'ttft_ms']
            : cdfMetric === 'tpot'
                ? ['tpot', 'tpot_ms']
                : cdfMetric === 'e2e'
                    ? ['e2e', 'e2e_ms']
                    : ['drift', 'drift_ms'];
        if (Array.isArray(cdfMap.quantiles)) {
            const values = cdfMap[keyForMetric[1]] || cdfMap[keyForMetric[0]];
            if (Array.isArray(values)) {
                return cdfMap.quantiles
                    .map((pct, idx) => ({ value: Number(values[idx]), pct: Number(pct) }))
                    .filter((pt) => Number.isFinite(pt.value) && Number.isFinite(pt.pct));
            }
        }
        const raw = Array.isArray(cdfMap[keyForMetric[0]])
            ? cdfMap[keyForMetric[0]]
            : Array.isArray(cdfMap[keyForMetric[1]])
                ? cdfMap[keyForMetric[1]]
                : [];
        return raw.map((pt) => {
            if (Array.isArray(pt)) {
                return { value: Number(pt[0]), pct: Number(pt[1]) };
            }
            return {
                value: Number(pt.value ?? pt.ms ?? pt.x ?? 0),
                pct: Number(pt.percentile ?? pt.pct ?? pt.y ?? 0),
            };
        }).filter((pt) => Number.isFinite(pt.value) && Number.isFinite(pt.pct));
    }, [cdfMap, cdfMetric]);

    const cdfSloMs = useMemo(() => {
        if (cdfMetric === 'ttft') {
            const s = numberAt(backendOptions, ['ttft_slo']);
            return s !== null ? s * 1000 : null;
        }
        if (cdfMetric === 'tpot') {
            const s = numberAt(backendOptions, ['tpot_slo']);
            return s !== null ? s * 1000 : null;
        }
        if (cdfMetric === 'e2e') {
            const s = numberAt(backendOptions, ['e2e_slo']);
            return s !== null ? s * 1000 : null;
        }
        return null;
    }, [backendOptions, cdfMetric]);

    // Percentile table rows
    const percentileRows = useMemo(() => {
        const ttft = normalizePercentiles(activeWindowData.ttft_ms || activeWindowData.ttft || summaryFallback.ttft || {});
        const tpot = normalizePercentiles(activeWindowData.tpot_ms || activeWindowData.tpot || summaryFallback.tpot || {});
        const e2e = normalizePercentiles(activeWindowData.e2e_ms || activeWindowData.e2e || activeWindowData.latency || summaryFallback.latency || {});
        const drift = driftStats;
        const inputLen = normalizePercentiles(activeWindowData.input_tokens || activeWindowData.input_length || summaryFallback.input_length || {});
        const outputLen = normalizePercentiles(activeWindowData.output_tokens || activeWindowData.output_length || summaryFallback.output_length || {});
        return [
            {
                label: 'TTFT',
                unit: 'ms',
                help: 'Time to First Token (ms) from client request dispatch to first streamed response token. High values indicate queueing or slow prefill.',
                formula: 'TTFT (ms) = t_first_token − t_request_start',
                stats: ttft,
            },
            {
                label: 'TPOT',
                unit: 'ms/tok',
                help: 'Time per Output Token (ms/tok), mean per-request decode latency per generated token after TTFT (distinct from per-step ITL). High values indicate decode memory-bandwidth pressure.',
                formula: 'TPOT (ms/tok) = (t_last_token − t_first_token) / max(1, Output_Tokens − 1)',
                note: activeWindowData.tpot_measured_ratio != null && activeWindowData.tpot_measured_ratio < 0.99
                    ? `${formatNumber(activeWindowData.tpot_measured_ratio * 100, '%')} measured`
                    : null,
                stats: tpot,
            },
            {
                label: 'E2E Latency',
                unit: 'ms',
                help: 'Total end-to-end request duration (ms) from send to final token completion, combining TTFT and full decode time.',
                formula: 'E2E (ms) = t_request_complete − t_request_start ≈ TTFT + TPOT × max(0, Output_Tokens − 1)',
                stats: e2e,
            },
            {
                label: 'Schedule Drift',
                unit: 'ms',
                help: 'Client dispatch delay (ms) between planned trace arrival timestamp and actual send time. Low drift confirms accurate replay pacing.',
                formula: 'Schedule_Drift (ms) = (t_actual_send − t_target_schedule) × 1000',
                stats: drift,
            },
            {
                label: 'Prompt Tokens',
                unit: 'tok',
                help: 'Prompt token count per request sent to the model endpoint. Longer prompts increase prefill compute and KV cache usage.',
                formula: 'Prompt_Tokens = TokenCount(Request_Prompt)',
                stats: inputLen,
            },
            {
                label: 'Output Tokens',
                unit: 'tok',
                help: 'Generated completion token count per request returned by the model. Longer outputs increase decode residency and KV retention time.',
                formula: 'Output_Tokens = TokenCount(Generated_Completion)',
                stats: outputLen,
            },
        ];
    }, [activeWindowData, driftStats, summaryFallback]);

    // Error breakdown
    const errorBreakdown = useMemo(() => {
        const raw = clientSeries.error_breakdown
            || clientSeries.error_summary?.breakdown
            || clientSeries.error_summary
            || clientSeries.status_code_breakdown
            || activeWindowData.error_breakdown
            || summaryFallback.status_code_breakdown
            || [];
        if (Array.isArray(raw)) return raw;
        if (raw && typeof raw === 'object' && raw.counts_by_code && typeof raw.counts_by_code === 'object') {
            const counts = raw.counts_by_code;
            const numericCodes = Object.keys(counts).filter((code) => /^\d{3}$/.test(code) && Number(counts[code]) > 0);
            const has4xx = numericCodes.some((code) => code.startsWith('4'));
            const has5xx = numericCodes.some((code) => code.startsWith('5'));
            const entries = Object.entries(counts).filter(([code, count]) => {
                if (code === 'all' || Number(count) <= 0) return false;
                if (code === '4xx' && has4xx) return false;
                if (code === '5xx' && has5xx) return false;
                return true;
            });
            const totalErr = Number(raw.total_errors ?? 0)
                || entries.reduce((acc, [, v]) => acc + Number(v || 0), 0);
            return entries.map(([code, count]) => ({
                filter_code: code,
                status_code: /^\d{3}$/.test(code) ? Number(code) : null,
                category: /^\d{3}$/.test(code) ? null : code,
                count: Number(count),
                percentage: totalErr > 0 ? (Number(count) / totalErr) * 100 : null,
            }));
        }
        return [];
    }, [activeWindowData.error_breakdown, clientSeries.error_breakdown, clientSeries.error_summary, clientSeries.status_code_breakdown, summaryFallback.status_code_breakdown]);

    return (
        <div className="space-y-6">
            {/* Trace Replay Fidelity */}
            <div className="grid gap-6 xl:grid-cols-2">
                <ChartContainer
                    title="Trace Replay Fidelity · Rate"
                    subtitle={fidelitySubtitle}
                    help="Compares target trace arrival rate (req/s) against actual client dispatch rate (req/s) across the send window. Divergence indicates client pacing limits or connection backpressure."
                    helpFormula="Target_RPS(t) = Trace_Arrivals_in_bin / Δt;  Actual_RPS(t) = Dispatched_Requests_in_bin / Δt"
                    helpAlign="start"
                    actions={hasGraceDrain ? (
                        <span className="inline-flex items-center gap-1">
                            <Badge tone="info" size="xs">
                                Send: 0–{dispatchDurationText}s
                            </Badge>
                            <HelpTooltip
                                label="Send window badge help"
                                align="end"
                                iconSize={11}
                                formula="Send_Window = [0, t_last_dispatch];  Grace_Drain = (t_last_dispatch, t_last_complete]"
                            >
                                Active trace dispatch window during which new requests are sent before the grace-drain period lets in-flight requests finish.
                            </HelpTooltip>
                        </span>
                    ) : null}
                >
                    {hasReplayRateSeries ? (
                        <>
                            <div className="h-56 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <LineChart data={rawBuckets} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis dataKey="t" type="number" domain={fidelityXDomain} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                        <YAxis width={52} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: 'req/s', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                        <Tooltip cursor={LINE_TOOLTIP_CURSOR} content={<RpsTooltip />} />
                                        <Line type="monotone" dataKey="target_rps" name="Target arrival" stroke={CHART_SERIES[1]} strokeWidth={2} strokeDasharray="4 3" connectNulls dot={sparseDotProp(rawBuckets, 'target_rps', CHART_SERIES[1])} isAnimationActive={false} />
                                        <Line type="monotone" dataKey="actual_rps" name="Actual send" stroke={CHART_SERIES[0]} strokeWidth={2} connectNulls dot={sparseDotProp(rawBuckets, 'actual_rps', CHART_SERIES[0])} isAnimationActive={false} />
                                    </LineChart>
                                </ResponsiveContainer>
                            </div>
                            <ChartLegend
                                className="mt-3"
                                entries={[
                                    { label: 'Target arrival (req/s)', color: CHART_SERIES[1] },
                                    { label: 'Actual send (req/s)', color: CHART_SERIES[0] },
                                ]}
                            />
                        </>
                    ) : (
                        <EmptyState title="Replay rate timeline unavailable" message="Per-second target and actual replay rates were not recorded." />
                    )}
                </ChartContainer>

                <ChartContainer
                    title="Schedule Drift (ms)"
                    subtitle={driftSubtitle}
                    help="Measures client dispatch skew (ms) between scheduled trace timestamps and actual request send times. Near-zero drift confirms accurate trace timing; spikes above 250 ms suggest client saturation."
                    helpFormula="Schedule_Drift (ms) = (t_actual_send − t_target_schedule) × 1000"
                    helpAlign="end"
                    actions={(
                        <div className="flex flex-wrap items-center gap-2">
                            <span className="inline-flex items-center gap-1">
                                <Badge tone="neutral" size="xs">P50: {formatDriftMs(driftStats.p50)}</Badge>
                                <HelpTooltip
                                    label="Schedule drift P50 help"
                                    align="end"
                                    iconSize={11}
                                    formula="Drift_P50 = Quantile_0.50(Schedule_Drift_ms)"
                                >
                                    Median dispatch timing drift (ms) across requests; values near 0 ms indicate accurate pacing.
                                </HelpTooltip>
                            </span>
                            <span className="inline-flex items-center gap-1">
                                <Badge tone={driftStats.p90 != null && driftStats.p90 > 250 ? 'warning' : 'success'} size="xs">
                                    P90: {formatDriftMs(driftStats.p90)}
                                </Badge>
                                <HelpTooltip
                                    label="Schedule drift P90 help"
                                    align="end"
                                    iconSize={11}
                                    formula="Drift_P90 = Quantile_0.90(Schedule_Drift_ms)"
                                >
                                    90th percentile dispatch drift (ms); values exceeding 250 ms indicate client-side scheduling lag during bursts.
                                </HelpTooltip>
                            </span>
                            <span className="inline-flex items-center gap-1">
                                <Badge tone="neutral" size="xs">Max: {formatDriftMs(driftStats.max)}</Badge>
                                <HelpTooltip
                                    label="Schedule drift Max help"
                                    align="end"
                                    iconSize={11}
                                    formula="Drift_Max = max(Schedule_Drift_ms)"
                                >
                                    Peak dispatch timing drift (ms) observed on the most delayed request in the window.
                                </HelpTooltip>
                            </span>
                        </div>
                    )}
                >
                    {hasDriftSeries ? (
                        <>
                            <div className="h-56 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <LineChart data={rawBuckets} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis dataKey="t" type="number" domain={fidelityXDomain} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                        <YAxis width={56} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: 'ms', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                        <Tooltip cursor={LINE_TOOLTIP_CURSOR} content={<MsTooltip />} />
                                        <Line type="monotone" dataKey="drift_p50" name="Drift P50" stroke={CHART_SERIES[0]} strokeWidth={2} connectNulls dot={sparseDotProp(rawBuckets, 'drift_p50', CHART_SERIES[0])} isAnimationActive={false} />
                                        <Line type="monotone" dataKey="drift_p90" name="Drift P90" stroke={CHART_SERIES[2]} strokeWidth={2} connectNulls dot={sparseDotProp(rawBuckets, 'drift_p90', CHART_SERIES[2])} isAnimationActive={false} />
                                        <Line type="monotone" dataKey="drift_p99" name="Drift P99" stroke={CHART_SERIES[4]} strokeWidth={1.5} strokeDasharray="4 3" connectNulls dot={sparseDotProp(rawBuckets, 'drift_p99', CHART_SERIES[4])} isAnimationActive={false} />
                                    </LineChart>
                                </ResponsiveContainer>
                            </div>
                            <ChartLegend
                                className="mt-3"
                                entries={[
                                    { label: 'Drift P50', color: CHART_SERIES[0] },
                                    { label: 'Drift P90', color: CHART_SERIES[2] },
                                    { label: 'Drift P99', color: CHART_SERIES[4] },
                                ]}
                            />
                        </>
                    ) : driftHistogram.length > 0 ? (
                        <div className="h-56 w-full">
                            <ResponsiveContainer width="100%" height="100%">
                                <BarChart data={driftHistogram} barCategoryGap="25%" margin={{ top: 8, right: 16, left: 0, bottom: 4 }}>
                                    <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                    <XAxis dataKey="label" tick={AXIS_TICK} stroke="var(--border-color)" />
                                    <YAxis width={52} allowDecimals={false} tick={AXIS_TICK} stroke="var(--border-color)" />
                                    <Tooltip cursor={BAR_TOOLTIP_CURSOR} content={<CountTooltip />} />
                                    <Bar dataKey="count" name="Requests" fill={CHART_SERIES[2]} radius={[6, 6, 0, 0]} maxBarSize={80} isAnimationActive={false} />
                                </BarChart>
                            </ResponsiveContainer>
                        </div>
                    ) : (
                        <EmptyState title="Schedule drift unavailable" message="No schedule drift measurements were recorded for this run." />
                    )}
                </ChartContainer>
            </div>

            {hasDriftSeries && driftHistogram.length > 0 && (
                <ChartContainer
                    title="Schedule Drift Distribution"
                    subtitle="Request count histogram by schedule drift bucket (ms)"
                    help="Distribution of request counts binned by schedule drift (ms). A tight cluster near 0 ms shows faithful trace replay; a long tail indicates dispatch queueing."
                    helpFormula="Bin_Count = Σ 1[bin_lo ≤ (t_actual_send − t_target_schedule) × 1000 < bin_hi]"
                    helpAlign="start"
                >
                    <div className="h-44 w-full">
                        <ResponsiveContainer width="100%" height="100%">
                            <BarChart data={driftHistogram} barCategoryGap="25%" margin={{ top: 8, right: 16, left: 0, bottom: 4 }}>
                                <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                <XAxis dataKey="label" tick={AXIS_TICK} stroke="var(--border-color)" />
                                <YAxis width={52} allowDecimals={false} tick={AXIS_TICK} stroke="var(--border-color)" />
                                <Tooltip cursor={BAR_TOOLTIP_CURSOR} content={<CountTooltip />} />
                                <Bar dataKey="count" name="Requests" fill={CHART_SERIES[2]} radius={[6, 6, 0, 0]} maxBarSize={80} isAnimationActive={false} />
                            </BarChart>
                        </ResponsiveContainer>
                    </div>
                </ChartContainer>
            )}

            {/* Sessions over Time */}
            <ChartContainer
                title="Sessions over Time"
                subtitle={slotSwimlaneSubtitle}
                help="Each horizontal row (S1..Sn) is a concurrent execution slot reused as soon as its prior request completes, colored by prompt prefix-cache hit tier (cold <10%, partial 10–70%, warm ≥70%)."
                helpFormula="Greedy_Slot_Packing([t_arrival, t_complete]);  Tier: Cold < 10%, Partial 10–70%, Warm ≥ 70%"
                helpAlign="start"
                actions={sessions.available && correlation.total_count != null && (
                    <span className="inline-flex items-center gap-1">
                        <Badge tone={Number(correlation.matched_ratio ?? 0) >= 0.8 ? 'success' : 'warning'} size="sm">
                            Correlated: {formatCount(correlation.matched_count)} / {formatCount(correlation.total_count)} ({formatNumber((correlation.matched_ratio ?? 0) * 100, '%')})
                        </Badge>
                        <HelpTooltip
                            label="Session correlation help"
                            align="end"
                            iconSize={12}
                            formula="Matched_Ratio (%) = (Matched_Session_Requests / Total_Requests) × 100"
                        >
                            Fraction of executed requests successfully matched to recorded session IDs for multi-turn swimlane and cache-bucket analysis.
                        </HelpTooltip>
                    </span>
                )}
            >
                {slotLanesData && slotLanesData.items.length > 0 ? (
                    <SlotSwimlaneChart slotData={slotLanesData} warmupSeconds={warmupSeconds} />
                ) : (
                    <EmptyState
                        icon={<GitBranch className="h-6 w-6" />}
                        title="Session swimlane unavailable"
                        message={sessions.reason || 'Session identifiers are not present in this trace format.'}
                    />
                )}
            </ChartContainer>

            {/* Latency by Input Sequence Length & Cache-Hit Bucket */}
            <div className="grid gap-6 xl:grid-cols-2">
                <ChartContainer
                    title="TTFT by Input Sequence Length Bucket"
                    subtitle="TTFT P50 and P90 across input sequence length buckets (tokens)"
                    help="TTFT P50 and P90 (ms) grouped by input sequence length bucket (<512 to >32K). Steep growth across larger buckets reflects prefill compute scaling and KV allocation cost."
                    helpFormula="Bucket_Pq = Quantile_q(TTFT_ms | Prompt_Tokens ∈ Bucket)  for q ∈ {0.50, 0.90}"
                    helpAlign="start"
                >
                    {promptLengthBuckets.length > 0 ? (
                        <>
                            <div className="h-56 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <BarChart data={promptLengthBuckets} barCategoryGap="25%" margin={{ top: 8, right: 16, left: 0, bottom: 4 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis dataKey="bucket" tick={AXIS_TICK} stroke="var(--border-color)" />
                                        <YAxis width={56} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: 'ms', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                        <Tooltip cursor={BAR_TOOLTIP_CURSOR} content={<MsTooltip />} />
                                        <Bar dataKey="p50" name="TTFT P50" fill={CHART_SERIES[0]} radius={[6, 6, 0, 0]} maxBarSize={50} isAnimationActive={false} />
                                        <Bar dataKey="p90" name="TTFT P90" fill={CHART_SERIES[1]} radius={[6, 6, 0, 0]} maxBarSize={50} isAnimationActive={false} />
                                    </BarChart>
                                </ResponsiveContainer>
                            </div>
                            <ChartLegend
                                className="mt-3"
                                entries={[
                                    { label: 'TTFT P50', color: CHART_SERIES[0] },
                                    { label: 'TTFT P90', color: CHART_SERIES[1] },
                                ]}
                            />
                        </>
                    ) : (
                        <EmptyState title="Input sequence length buckets unavailable" message="Per-request input sequence lengths were not available to bucket latency." />
                    )}
                </ChartContainer>

                <ChartContainer
                    title="TTFT by Prefix Cache Hit Bucket"
                    subtitle="Cold (< 10%), Partial (10–70%), and Warm (≥ 70%) prefix-cache buckets · completed requests"
                    help="Compares TTFT P50 and P90 (ms) across completed requests grouped by prefix-cache hit tier (Cold < 10%, Partial 10–70%, Warm ≥ 70%), matching Sessions over Time. Lower Warm TTFT confirms effective KV prefix reuse; failed requests without valid TTFT are excluded."
                    helpFormula="Cache_Hit_% = (Matched_Prefix_Tokens / Prompt_Tokens) × 100;  Buckets: Cold < 10%, Partial 10–70%, Warm ≥ 70%  (completed requests)"
                    helpAlign="end"
                >
                    {cacheBuckets.length > 0 ? (
                        <>
                            <div className="h-56 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <BarChart data={cacheBuckets} barCategoryGap="25%" margin={{ top: 8, right: 16, left: 0, bottom: 4 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis
                                            dataKey="bucket"
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                            tickFormatter={(v) => {
                                                const key = String(v || '').toLowerCase();
                                                if (key === 'cold') return 'Cold (<10%)';
                                                if (key === 'partial') return 'Partial (10–70%)';
                                                if (key === 'warm') return 'Warm (≥70%)';
                                                return displayName(v);
                                            }}
                                        />
                                        <YAxis width={56} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: 'ms', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                        <Tooltip cursor={BAR_TOOLTIP_CURSOR} content={<MsTooltip />} />
                                        <Bar dataKey="p50" name="TTFT P50" fill={CHART_SERIES[0]} radius={[6, 6, 0, 0]} maxBarSize={50} isAnimationActive={false} />
                                        <Bar dataKey="p90" name="TTFT P90" fill={CHART_SERIES[2]} radius={[6, 6, 0, 0]} maxBarSize={50} isAnimationActive={false} />
                                    </BarChart>
                                </ResponsiveContainer>
                            </div>
                            <ChartLegend
                                className="mt-3"
                                entries={[
                                    { label: 'TTFT P50', color: CHART_SERIES[0] },
                                    { label: 'TTFT P90', color: CHART_SERIES[2] },
                                ]}
                            />
                        </>
                    ) : (
                        <EmptyState
                            title="Prefix cache hit buckets unavailable"
                            message="Prefix hash_ids or multi-turn session metadata are required to classify cold (<10%), partial (10–70%), and warm (≥70%) buckets."
                        />
                    )}
                </ChartContainer>
            </div>

            {/* Per-Request Time × Sequence-Length Latency Scatter */}
            <div className="grid gap-6 xl:grid-cols-2">
                <LatencyHeatmap
                    title="TTFT by Time and Input Sequence Length"
                    subtitle="Per-request TTFT (ms) by elapsed time and input sequence length (tokens)"
                    metric="TTFT"
                    data={ttftHeatmap}
                    help="Per-request Time to First Token (ms) plotted by elapsed run time (X-axis) and input sequence length in tokens (Y-axis). Each dot is one request colored by TTFT; warmer dots expose prefill bottlenecks on long prompts or burst intervals."
                    helpFormula="Point_i = (t_i, ISL_i, TTFT_i);  TTFT_i = t_first_token,i − t_request_start,i"
                />
                <LatencyHeatmap
                    title="TPOT by Time and Output Sequence Length"
                    subtitle={tpotHeatmap?.sequence_length_source === 'requested'
                        ? 'Per-request TPOT (ms/tok) by elapsed time and requested output sequence length (tokens)'
                        : 'Per-request TPOT (ms/tok) by elapsed time and output sequence length (tokens)'}
                    metric="TPOT"
                    data={tpotHeatmap}
                    help={tpotHeatmap?.sequence_length_source === 'requested'
                        ? 'Per-request Time per Output Token (ms/tok) plotted by elapsed run time (X-axis) and requested output sequence length in tokens (Y-axis). Each dot is one request colored by TPOT; warmer dots highlight decode slowdown as target generation length grows.'
                        : 'Per-request Time per Output Token (ms/tok) plotted by elapsed run time (X-axis) and output sequence length in tokens (Y-axis). Each dot is one request colored by TPOT; warmer dots highlight decode slowdown as active context grows.'}
                    helpFormula="Point_i = (t_i, OSL_i, TPOT_i);  TPOT_i = (t_last_token,i − t_first_token,i) / max(1, Output_Tokens_i − 1)"
                />
            </div>

            {/* Conversation Structure */}
            {sessions.available && (turnsHistogram.length > 0 || depthHistogram.length > 0) && (
                <div className="grid gap-6 xl:grid-cols-2">
                    <ChartContainer
                        title="Turns per Session"
                        subtitle="Conversation turn count distribution"
                        help="Histogram of conversation turn counts across sessions. Multi-turn sessions increase prefix-cache reuse potential while extending KV-cache residency."
                        helpFormula="Turn_Hist(k) = Count(Sessions with Total_Turns = k)"
                        helpAlign="start"
                    >
                        {turnsHistogram.length > 0 ? (
                            <div className="h-52 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <BarChart data={turnsHistogram} barCategoryGap="25%" margin={{ top: 8, right: 16, left: 0, bottom: 18 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis
                                            dataKey="label"
                                            tickFormatter={formatTurnLabel}
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                            label={{ value: 'Turns per session', position: 'insideBottom', offset: -10, fill: 'var(--text-muted)', fontSize: 10 }}
                                        />
                                        <YAxis
                                            width={56}
                                            allowDecimals={false}
                                            tick={AXIS_TICK}
                                            stroke="var(--border-color)"
                                            label={{ value: 'Sessions', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }}
                                        />
                                        <Tooltip cursor={BAR_TOOLTIP_CURSOR} content={<SessionsTooltip />} />
                                        <Bar dataKey="count" name="Sessions" fill={CHART_SERIES[0]} radius={[6, 6, 0, 0]} maxBarSize={80} isAnimationActive={false} />
                                    </BarChart>
                                </ResponsiveContainer>
                            </div>
                        ) : (
                            <EmptyState title="No turn distribution" message="Turn counts were not recorded." />
                        )}
                    </ChartContainer>

                    <ChartContainer
                        title="Conversation Tree Depth"
                        subtitle="Parent-child chat tree depth distribution"
                        help="Distribution of parent-child branching depths across conversation threads. Deeper trees indicate shared history prefixes suited for prefix-aware routing."
                        helpFormula="Depth_Hist(d) = Count(Threads with Max_Branch_Depth = d)"
                        helpAlign="end"
                    >
                        {depthHistogram.length > 0 ? (
                            <div className="h-52 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <BarChart data={depthHistogram} barCategoryGap="25%" margin={{ top: 8, right: 16, left: 0, bottom: 4 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis dataKey="label" tick={AXIS_TICK} stroke="var(--border-color)" />
                                        <YAxis width={48} allowDecimals={false} tick={AXIS_TICK} stroke="var(--border-color)" />
                                        <Tooltip cursor={BAR_TOOLTIP_CURSOR} content={<CountTooltip />} />
                                        <Bar dataKey="count" name="Threads" fill={CHART_SERIES[3]} radius={[6, 6, 0, 0]} maxBarSize={80} isAnimationActive={false} />
                                    </BarChart>
                                </ResponsiveContainer>
                            </div>
                        ) : (
                            <EmptyState title="No tree depth data" message="Parent chat hierarchy was not present in this trace." />
                        )}
                    </ChartContainer>
                </div>
            )}

            {/* CDF & Full Percentile Table */}
            <ChartContainer
                title="Cumulative Distribution Function (CDF)"
                subtitle={`Interactive percentile distribution (${analysisWindow === 'full' ? 'Full Run' : (hasWindowCdf || !clientSeries.cdf ? 'Profiling Steady-State' : 'Full Run fallback — legacy data')})`}
                help="Empirical cumulative distribution (%) of requests completing at or below each latency (ms or ms/tok) or schedule drift threshold (ms), with optional SLO reference line. Curves shifted left indicate tighter latency."
                helpFormula="F(x) = (1 / N) Σ 1[x_i ≤ x] × 100%"
                helpAlign="start"
                actions={(
                    <ToggleGroup
                        size="xs"
                        value={cdfMetric}
                        onChange={setCdfMetric}
                        options={[
                            { value: 'ttft', label: 'TTFT' },
                            { value: 'tpot', label: 'TPOT' },
                            { value: 'e2e', label: 'E2E' },
                            { value: 'drift', label: 'Schedule Drift' },
                        ]}
                    />
                )}
            >
                {cdfPoints.length > 0 ? (
                    <div className="h-60 w-full">
                        <ResponsiveContainer width="100%" height="100%">
                            <LineChart data={cdfPoints} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                <XAxis dataKey="value" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)} ${cdfMetric === 'tpot' ? 'ms/tok' : 'ms'}`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                <YAxis domain={[0, 100]} width={48} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: '%', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                <Tooltip cursor={LINE_TOOLTIP_CURSOR} content={cdfMetric === 'tpot' ? <TpotCdfTooltip /> : <CdfTooltip />} />
                                {cdfSloMs !== null && (
                                    <ReferenceLine x={cdfSloMs} stroke={CHART_SERIES[2]} strokeDasharray="4 3" />
                                )}
                                <Line type="monotone" dataKey="pct" name="Cumulative %" stroke={CHART_SERIES[0]} strokeWidth={2} dot={sparseDotProp(cdfPoints, 'pct', CHART_SERIES[0])} isAnimationActive={false} />
                            </LineChart>
                        </ResponsiveContainer>
                    </div>
                ) : (
                    <EmptyState title="CDF unavailable" message="Percentile distribution curve points were not computed for this metric." />
                )}
            </ChartContainer>

            <Panel
                title={`Full Percentile Table · ${analysisWindow === 'full' ? 'Full Run' : 'Profiling Steady-State'}`}
                help="Complete summary percentiles across the active analysis window for latency (ms or ms/tok), schedule drift (ms), and token counts. Compare P50 to P99/Max to spot tail amplification."
                helpFormula="Avg = (1/N) Σ x_i;  Pq = Quantile_q(sorted(x));  Min = min(x_i);  Max = max(x_i)"
                helpAlign="start"
            >
                <div className="overflow-x-auto lg:overflow-visible">
                    <table className="w-full text-left text-xs">
                        <thead className="border-b border-theme-border text-[10px] uppercase tracking-wider text-theme-muted">
                            <tr>
                                <th className="py-2 pr-4">
                                    <span className="inline-flex items-center gap-1">
                                        <span>Metric</span>
                                        <HelpTooltip label="Percentile metric column help" align="start" iconSize={11}>
                                            Client latency, dispatch timing drift, or token length dimension summarized in each row.
                                        </HelpTooltip>
                                    </span>
                                </th>
                                <th className="px-3 py-2 text-right">
                                    <span className="inline-flex items-center justify-end gap-1">
                                        <span>Avg</span>
                                        <HelpTooltip label="Avg column help" align="end" iconSize={11} formula="Avg = (1 / N) Σ x_i">
                                            Arithmetic mean across all valid samples in the active analysis window.
                                        </HelpTooltip>
                                    </span>
                                </th>
                                <th className="px-3 py-2 text-right">
                                    <span className="inline-flex items-center justify-end gap-1">
                                        <span>Min</span>
                                        <HelpTooltip label="Min column help" align="end" iconSize={11} formula="Min = min(x_i)">
                                            Lowest observed sample value in the active analysis window.
                                        </HelpTooltip>
                                    </span>
                                </th>
                                <th className="px-3 py-2 text-right">
                                    <span className="inline-flex items-center justify-end gap-1">
                                        <span>P50</span>
                                        <HelpTooltip label="P50 column help" align="end" iconSize={11} formula="P50 = Quantile_0.50(sorted(x))">
                                            50th percentile (median); half of requests fall at or below this value.
                                        </HelpTooltip>
                                    </span>
                                </th>
                                <th className="px-3 py-2 text-right">
                                    <span className="inline-flex items-center justify-end gap-1">
                                        <span>P90</span>
                                        <HelpTooltip label="P90 column help" align="end" iconSize={11} formula="P90 = Quantile_0.90(sorted(x))">
                                            90th percentile; 90% of requests fall at or below this value.
                                        </HelpTooltip>
                                    </span>
                                </th>
                                <th className="px-3 py-2 text-right">
                                    <span className="inline-flex items-center justify-end gap-1">
                                        <span>P95</span>
                                        <HelpTooltip label="P95 column help" align="end" iconSize={11} formula="P95 = Quantile_0.95(sorted(x))">
                                            95th percentile; upper-tail threshold exceeded by 5% of requests.
                                        </HelpTooltip>
                                    </span>
                                </th>
                                <th className="px-3 py-2 text-right">
                                    <span className="inline-flex items-center justify-end gap-1">
                                        <span>P99</span>
                                        <HelpTooltip label="P99 column help" align="end" iconSize={11} formula="P99 = Quantile_0.99(sorted(x))">
                                            99th percentile; extreme tail latency affecting the slowest 1% of requests.
                                        </HelpTooltip>
                                    </span>
                                </th>
                                <th className="pl-3 py-2 text-right">
                                    <span className="inline-flex items-center justify-end gap-1">
                                        <span>Max</span>
                                        <HelpTooltip label="Max column help" align="end" iconSize={11} formula="Max = max(x_i)">
                                            Worst-case peak value observed across all requests in the active window.
                                        </HelpTooltip>
                                    </span>
                                </th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-theme-border">
                            {percentileRows.map((row) => {
                                const fmt = (v) => {
                                    if (row.unit === 'ms') return formatMilliseconds(v);
                                    if (row.unit === 'ms/tok') {
                                        const msText = formatMilliseconds(v);
                                        return msText === '—' ? '—' : `${msText}/tok`;
                                    }
                                    return formatCount(v);
                                };
                                return (
                                    <tr key={row.label} className="text-theme-text">
                                        <td className="py-2.5 pr-4 font-semibold">
                                            <span className="inline-flex items-center gap-1.5">
                                                <span>{row.label}</span>
                                                <HelpTooltip label={`${row.label} metric help`} align="start" iconSize={11} formula={row.formula}>
                                                    {row.help}
                                                </HelpTooltip>
                                                {row.note && (
                                                    <span className="text-[10px] font-normal text-theme-muted">({row.note})</span>
                                                )}
                                            </span>
                                        </td>
                                        <td className="px-3 py-2.5 text-right font-mono">{fmt(row.stats.avg)}</td>
                                        <td className="px-3 py-2.5 text-right font-mono">{fmt(row.stats.min)}</td>
                                        <td className="px-3 py-2.5 text-right font-mono">{fmt(row.stats.p50)}</td>
                                        <td className="px-3 py-2.5 text-right font-mono">{fmt(row.stats.p90)}</td>
                                        <td className="px-3 py-2.5 text-right font-mono">{fmt(row.stats.p95)}</td>
                                        <td className="px-3 py-2.5 text-right font-mono">{fmt(row.stats.p99)}</td>
                                        <td className="pl-3 py-2.5 text-right font-mono">{fmt(row.stats.max)}</td>
                                    </tr>
                                );
                            })}
                        </tbody>
                    </table>
                </div>
            </Panel>

            {/* Error Breakdown Table & Drill-Down */}
            <Panel
                title="Error Breakdown & Drill-Down"
                help="Failed requests grouped by HTTP status code or transport error category. Click a row or Drill down to inspect failing request payloads and timestamps."
                helpFormula="Total_Errors = Σ Count(Status_Code_or_Error_Category)"
                helpAlign="start"
                actions={onOpenErrorInspector && (
                    <Button variant="secondary" size="sm" onClick={() => onOpenErrorInspector('all')}>
                        <Search className="h-3.5 w-3.5" aria-hidden="true" />
                        Inspect Errors
                    </Button>
                )}
            >
                {errorBreakdown.length > 0 ? (
                    <div className="overflow-x-auto lg:overflow-visible">
                        <table className="w-full text-left text-xs">
                            <thead className="border-b border-theme-border text-[10px] uppercase tracking-wider text-theme-muted">
                                <tr>
                                    <th className="py-2 pr-4">
                                        <span className="inline-flex items-center gap-1">
                                            <span>Category / Code</span>
                                            <HelpTooltip label="Error category column help" align="start" iconSize={11}>
                                                HTTP response status code (for example 503 or 429) or client transport failure category (such as timeout).
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2 text-right">
                                        <span className="inline-flex items-center justify-end gap-1">
                                            <span>Count</span>
                                            <HelpTooltip label="Error count column help" align="end" iconSize={11} formula="Count_c = Σ 1[Error_Category_i = c]">
                                                Total number of failed requests recorded for this status code or error category.
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2 text-right">
                                        <span className="inline-flex items-center justify-end gap-1">
                                            <span>Share</span>
                                            <HelpTooltip label="Error share column help" align="end" iconSize={11} formula="Share_c (%) = (Count_c / Total_Errors) × 100">
                                                Percentage of total failed requests represented by this error type.
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2">
                                        <span className="inline-flex items-center gap-1">
                                            <span>Guidance</span>
                                            <HelpTooltip label="Error guidance column help" align="start" iconSize={11}>
                                                Likely cause and suggested remediation step for this failure status.
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="pl-3 py-2 text-right">
                                        <span className="inline-flex items-center justify-end gap-1">
                                            <span>Action</span>
                                            <HelpTooltip label="Error action column help" align="end" iconSize={11}>
                                                Opens the error inspector filtered to requests matching this status code or category.
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-theme-border">
                                {errorBreakdown.map((item, idx) => {
                                    const rawCode = item.filter_code ?? item.status_code ?? item.category ?? 'unknown';
                                    const filterCode = String(rawCode);
                                    const label = item.status_code != null
                                        ? `HTTP ${item.status_code}`
                                        : formatErrorCategory(item.category || rawCode);
                                    return (
                                        <tr
                                            key={`${filterCode}-${idx}`}
                                            onClick={() => onOpenErrorInspector?.(filterCode)}
                                            className={cn(
                                                'text-theme-text transition-colors',
                                                onOpenErrorInspector && 'cursor-pointer hover:bg-slate-100/60 dark:hover:bg-slate-800/40',
                                            )}
                                        >
                                            <td className="py-2.5 pr-4">
                                                <Badge tone="danger" size="sm">{label}</Badge>
                                            </td>
                                            <td className="px-3 py-2.5 text-right font-mono font-semibold">{formatCount(item.count)}</td>
                                            <td className="px-3 py-2.5 text-right font-mono">{formatNumber(item.percentage, '%')}</td>
                                            <td className="px-3 py-2.5 text-theme-muted">
                                                {item.guidance || responseStatusGuidance(item.status_code)}
                                            </td>
                                            <td className="pl-3 py-2.5 text-right">
                                                <Button
                                                    variant="ghost"
                                                    size="xs"
                                                    onClick={(event) => {
                                                        event.stopPropagation();
                                                        onOpenErrorInspector?.(filterCode);
                                                    }}
                                                >
                                                    Drill down
                                                </Button>
                                            </td>
                                        </tr>
                                    );
                                })}
                            </tbody>
                        </table>
                    </div>
                ) : (
                    <EmptyState
                        icon={<AlertTriangle className="h-6 w-6" />}
                        tone="success"
                        title="No request errors recorded"
                        message="All requests in this run completed without HTTP or transport errors."
                    />
                )}
            </Panel>
        </div>
    );
}

export default RequestsTab;
