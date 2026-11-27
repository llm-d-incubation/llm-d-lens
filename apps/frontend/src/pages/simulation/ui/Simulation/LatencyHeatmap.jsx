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
import { cn } from '../../../../shared/lib/cn';
import { ChartContainer, ChartTooltip, ChartTooltipRow, EmptyState } from '../../../../shared/ui';
import { formatCount, formatDecimal, formatMilliseconds, formatNumber } from '../../model/presentation';

const HEATMAP_STOPS = [
    [14, 165, 233],
    [16, 185, 129],
    [245, 158, 11],
    [244, 63, 94],
];

const HEATMAP_GRADIENT = `linear-gradient(90deg, ${HEATMAP_STOPS.map(([r, g, b]) => `rgb(${r}, ${g}, ${b})`).join(', ')})`;
const SNAP_RADIUS_PX = 18;
const SNAP_RADIUS_SQ = SNAP_RADIUS_PX * SNAP_RADIUS_PX;

function finiteMetric(value) {
    if (value === null || value === undefined || typeof value === 'boolean') return null;
    if (typeof value === 'string' && value.trim() === '') return null;
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : null;
}

function heatmapColor(value) {
    const ratio = Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0;
    const position = ratio * (HEATMAP_STOPS.length - 1);
    const startIndex = Math.min(Math.floor(position), HEATMAP_STOPS.length - 2);
    const progress = position - startIndex;
    const startStop = HEATMAP_STOPS[startIndex];
    const endStop = HEATMAP_STOPS[startIndex + 1];
    const rgb = startStop.map((channel, index) => (
        Math.round(channel + (endStop[index] - channel) * progress)
    ));
    return `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`;
}

function pointerToSvgCoords(event, svgEl, viewBoxWidth, viewBoxHeight) {
    const clientX = event?.clientX;
    const clientY = event?.clientY;
    if (!Number.isFinite(clientX) || !Number.isFinite(clientY)) return null;

    if (svgEl && typeof svgEl.getScreenCTM === 'function') {
        const ctm = svgEl.getScreenCTM();
        if (ctm && typeof ctm.inverse === 'function') {
            const inv = ctm.inverse();
            if (typeof DOMPoint === 'function') {
                const mapped = new DOMPoint(clientX, clientY).matrixTransform(inv);
                if (Number.isFinite(mapped.x) && Number.isFinite(mapped.y)) {
                    return { svgX: mapped.x, svgY: mapped.y };
                }
            } else if (typeof svgEl.createSVGPoint === 'function') {
                const pt = svgEl.createSVGPoint();
                pt.x = clientX;
                pt.y = clientY;
                const mapped = pt.matrixTransform(inv);
                if (Number.isFinite(mapped.x) && Number.isFinite(mapped.y)) {
                    return { svgX: mapped.x, svgY: mapped.y };
                }
            }
        }
    }

    const rect = svgEl?.getBoundingClientRect?.();
    if (rect && rect.width > 0 && rect.height > 0) {
        return {
            svgX: ((clientX - rect.left) / rect.width) * viewBoxWidth,
            svgY: ((clientY - rect.top) / rect.height) * viewBoxHeight,
        };
    }
    return null;
}

export function LatencyHeatmap({ title, subtitle, metric, data, help, helpFormula }) {
    const containerRef = useRef(null);
    const svgRef = useRef(null);
    const [hovered, setHovered] = useState(null);
    const [measuredWidth, setMeasuredWidth] = useState(null);

    const rawPoints = data?.points;
    const validPoints = useMemo(() => {
        const list = Array.isArray(rawPoints) ? rawPoints : [];
        return list
            .map((pt) => {
                if (!pt || typeof pt !== 'object') return null;
                const t = finiteMetric(pt.t ?? pt.elapsed_seconds ?? pt.time_seconds);
                const seq = finiteMetric(pt.seq ?? pt.sequence_length);
                const ms = finiteMetric(pt.ms ?? pt.value ?? pt.metric_ms);
                if (t === null || seq === null || ms === null) return null;
                if (t < 0 || seq < 0 || ms < 0) return null;
                return { t, seq, ms };
            })
            .filter(Boolean);
    }, [rawPoints]);

    const sortedPoints = useMemo(
        () => [...validPoints].sort((a, b) => (a.ms - b.ms) || (a.t - b.t)),
        [validPoints],
    );

    const cells = Array.isArray(data?.cells) ? data.cells : [];
    const hasPoints = sortedPoints.length > 0;
    const hasLegacyCells = !hasPoints && cells.length > 0;
    const hasContent = hasPoints || hasLegacyCells;

    useEffect(() => {
        if (!hasContent) return undefined;
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
    }, [hasContent]);

    const upperMetric = String(metric || '').toUpperCase();
    const isTpot = upperMetric === 'TPOT';
    const isTtft = upperMetric === 'TTFT';
    const formatLatencyValue = (value) => {
        const base = formatMilliseconds(value);
        return isTpot && base !== '—' ? `${base}/tok` : base;
    };

    const sequenceType = String(data?.sequence_length || '').trim().toLowerCase();
    const sequenceSource = String(data?.sequence_length_source || '').trim().toLowerCase();
    const isInputSequence = sequenceType === 'input';
    const sequenceLabel = isInputSequence
        ? 'Input sequence length'
        : (sequenceSource === 'requested' ? 'Requested output sequence length' : 'Output sequence length');
    const yAxisHelpLabel = (isInputSequence || sequenceType === 'output' || sequenceSource)
        ? sequenceLabel.toLowerCase()
        : 'sequence length';

    const panelHelp = help ?? (
        hasLegacyCells
            ? `Bins per-request ${metric} latency (${isTpot ? 'ms/tok' : 'ms'}) by elapsed replay time (X-axis) and ${yAxisHelpLabel} in tokens (Y-axis). Warmer cells indicate higher latency hotspots.`
            : `Plots each request's ${metric} latency (${isTpot ? 'ms/tok' : 'ms'}) by elapsed replay time (X-axis) and ${yAxisHelpLabel} in tokens (Y-axis). Warmer dots indicate higher latency.`
    );
    const defaultFormula = hasLegacyCells
        ? (isTpot
            ? 'Cell_Avg_TPOT = (1 / N_cell) Σ TPOT_i;  TPOT_i = (t_last_token − t_first_token) / max(1, Output_Tokens − 1)'
            : `Cell_Avg_${metric} = (1 / N_cell) Σ ${metric}_i over (Elapsed_Time_bin, Seq_Length_bin)`)
        : (isTpot
            ? 'Point_i = (t_i, OSL_i, TPOT_i);  TPOT_i = (t_last_token,i − t_first_token,i) / max(1, Output_Tokens_i − 1)'
            : (isTtft
                ? 'Point_i = (t_i, ISL_i, TTFT_i);  TTFT_i = t_first_token,i − t_request_start,i'
                : `Point_i = (t_i, Seq_Len_i, ${metric}_i)`));
    const panelFormula = helpFormula ?? (help === undefined ? defaultFormula : undefined);
    const computedSubtitle = subtitle ?? (
        hasPoints
            ? `Per-request ${metric} across elapsed time and ${sequenceLabel.toLowerCase()}`
            : (hasLegacyCells
                ? `Average ${metric} across elapsed time and ${sequenceLabel.toLowerCase()}`
                : undefined)
    );

    if (!hasPoints && !hasLegacyCells) {
        return (
            <ChartContainer title={title} subtitle={computedSubtitle} help={panelHelp} helpFormula={panelFormula} helpAlign="start">
                <EmptyState
                    title={`${metric} scatter plot unavailable`}
                    message={`No per-request ${metric} and sequence-length data are available.`}
                />
            </ChartContainer>
        );
    }

    const svgWidth = Math.max(640, Math.round(measuredWidth || 800));
    const height = 256;
    const margin = { top: 12, right: 24, bottom: 44, left: 72 };
    const plotWidth = svgWidth - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    const metricName = String(metric || 'Latency').trim() || 'Latency';
    const activeHovered = hovered?.dataRef === data ? hovered : null;

    const buildHoverMeta = (event) => {
        const rect = containerRef.current?.getBoundingClientRect?.();
        if (!rect) {
            return { xPx: 24, yPx: 24, containerW: 640, containerH: 256 };
        }
        return {
            xPx: (Number.isFinite(event?.clientX) ? event.clientX : rect.left + 24) - rect.left,
            yPx: (Number.isFinite(event?.clientY) ? event.clientY : rect.top + 24) - rect.top,
            containerW: rect.width,
            containerH: rect.height,
        };
    };

    const handlePointHover = (point, color, event) => {
        setHovered({
            dataRef: data,
            point,
            color,
            ...buildHoverMeta(event),
        });
    };

    const handleCellHover = (cell, color, event) => {
        setHovered({
            dataRef: data,
            cell,
            color,
            ...buildHoverMeta(event),
        });
    };

    let x;
    let y;
    let xTicks;
    let yTicks;
    let formatXTick;
    let formatYTick;
    let xAxisLabel;
    let ariaLabel;
    let minimumMetric;
    let maximumMetric;
    let colorLegendPrefix;
    let summaryText;
    let plottedPoints = [];
    let dotRadius = 3.5;

    if (hasPoints) {
        const ptTimes = sortedPoints.map((pt) => pt.t);
        const minPtT = Math.min(...ptTimes);
        const maxPtT = Math.max(...ptTimes);
        const winStart = finiteMetric(data?.window_start_seconds);
        const dur = finiteMetric(data?.duration_seconds);
        const minT = winStart !== null && winStart > 0 ? Math.min(winStart, minPtT) : Math.min(0, minPtT);
        const rawMaxT = Math.max(maxPtT, dur !== null && dur >= 0 ? dur : 0);
        const maxT = rawMaxT > minT ? rawMaxT : minT + 1;
        const timeSpan = maxT - minT;

        const ptSeqs = sortedPoints.map((pt) => pt.seq);
        const minPtS = Math.min(...ptSeqs);
        const maxPtS = Math.max(...ptSeqs);
        const padS = maxPtS > minPtS
            ? Math.max(1, (maxPtS - minPtS) * 0.05)
            : Math.max(1, Math.round(minPtS * 0.1));
        const minS = Math.max(0, Math.floor(minPtS - padS));
        const rawMaxS = Math.ceil(maxPtS + padS);
        const spanS = Math.max(4, Math.ceil((rawMaxS - minS) / 4) * 4);
        const maxS = minS + spanS;

        const ptMetrics = sortedPoints.map((pt) => pt.ms);
        const ptMinV = Math.min(...ptMetrics);
        const ptMaxV = Math.max(...ptMetrics);
        const rawMinV = finiteMetric(data?.minimum_metric_ms ?? data?.min_ms);
        const rawMaxV = finiteMetric(data?.maximum_metric_ms ?? data?.max_ms);
        const hasValidExplicit = rawMinV !== null && rawMaxV !== null && rawMinV <= rawMaxV;
        minimumMetric = hasValidExplicit ? Math.min(rawMinV, ptMinV) : ptMinV;
        maximumMetric = hasValidExplicit ? Math.max(rawMaxV, ptMaxV) : ptMaxV;
        const metricRange = maximumMetric - minimumMetric;

        x = (value) => margin.left + ((Number(value) - minT) / timeSpan) * plotWidth;
        y = (value) => margin.top + ((maxS - Number(value)) / spanS) * plotHeight;
        xTicks = Array.from({ length: 5 }, (_, index) => minT + (timeSpan * index) / 4);
        yTicks = Array.from({ length: 5 }, (_, index) => minS + (spanS * index) / 4);
        formatXTick = (tick) => `${formatDecimal(tick)}s`;
        formatYTick = (tick) => formatCount(Math.round(tick));
        xAxisLabel = 'Elapsed time (s)';
        ariaLabel = `${metric} per-request scatter plot by elapsed time and ${sequenceLabel.toLowerCase()}`;
        colorLegendPrefix = `Color: ${metricName} (${isTpot ? 'ms/tok' : 'ms'})`;

        const renderedCount = sortedPoints.length;
        dotRadius = renderedCount > 800 ? 3.0 : renderedCount > 200 ? 3.5 : 4.2;
        plottedPoints = sortedPoints.map((pt) => {
            const normalized = metricRange > 0 ? (pt.ms - minimumMetric) / metricRange : 0;
            return {
                pt,
                cx: x(pt.t),
                cy: y(pt.seq),
                color: heatmapColor(normalized),
            };
        });

        const rawPointsTotal = finiteMetric(data?.points_total);
        const pointsTotal = rawPointsTotal !== null && rawPointsTotal >= renderedCount
            ? Math.trunc(rawPointsTotal)
            : renderedCount;
        summaryText = pointsTotal > renderedCount
            ? `Showing ${formatCount(renderedCount)} of ${formatCount(pointsTotal)} requests (uniformly sampled)`
            : `${formatCount(pointsTotal)} ${pointsTotal === 1 ? 'request' : 'requests'}`;
    } else {
        const maximumTime = Math.max(0.001, ...cells.map((cell) => Number(cell.end_seconds) || 0));
        const seqStarts = cells.map((cell) => finiteMetric(cell.sequence_length_start)).filter((v) => v !== null);
        const seqEnds = cells.map((cell) => finiteMetric(cell.sequence_length_end)).filter((v) => v !== null);
        const minimumSequence = seqStarts.length ? Math.min(...seqStarts) : 0;
        const maximumSequence = seqEnds.length ? Math.max(...seqEnds) : 1;
        const sequenceRange = Math.max(1, maximumSequence - minimumSequence);

        const averages = cells.map((cell) => finiteMetric(cell.average_metric_ms)).filter((v) => v !== null);
        minimumMetric = finiteMetric(data?.minimum_metric_ms ?? data?.min_ms) ?? (averages.length ? Math.min(...averages) : 0);
        maximumMetric = finiteMetric(data?.maximum_metric_ms ?? data?.max_ms) ?? (averages.length ? Math.max(...averages) : 0);

        x = (value) => margin.left + (Number(value) / maximumTime) * plotWidth;
        y = (value) => margin.top + ((maximumSequence - Number(value)) / sequenceRange) * plotHeight;
        xTicks = Array.from({ length: 5 }, (_, index) => (maximumTime * index) / 4);
        yTicks = Array.from({ length: 5 }, (_, index) => minimumSequence + (sequenceRange * index) / 4);
        formatXTick = (tick) => `${formatNumber(tick)}s`;
        formatYTick = (tick) => formatNumber(tick);
        xAxisLabel = 'Elapsed time';
        ariaLabel = `${metric} legacy binned heatmap by elapsed time and ${sequenceLabel.toLowerCase()}`;
        colorLegendPrefix = `Color: Avg ${metricName} (${isTpot ? 'ms/tok' : 'ms'})`;

        const totalRequests = cells.reduce(
            (acc, cell) => acc + (finiteMetric(cell.request_count) ?? 0),
            0,
        );
        const timeBinCount = finiteMetric(data?.time_bin_count ?? data?.time_bins);
        const seqBinCount = finiteMetric(data?.sequence_length_bin_count ?? data?.len_bins);
        const hasGridResolution = timeBinCount !== null && timeBinCount > 0
            && seqBinCount !== null && seqBinCount > 0;
        const legacyBaseSummary = hasGridResolution
            ? `${formatCount(timeBinCount)} × ${formatCount(seqBinCount)} grid · ${formatCount(totalRequests)} requests`
            : `${formatCount(totalRequests)} requests`;
        summaryText = `${legacyBaseSummary} (legacy binned fallback)`;
    }

    const metricRange = maximumMetric - minimumMetric;
    const isUniformMetric = metricRange <= 0;

    const handlePlotMouseMove = (event) => {
        if (!hasPoints) return;
        const coords = pointerToSvgCoords(event, svgRef.current, svgWidth, height);
        if (!coords) return;
        if (
            coords.svgX < margin.left - SNAP_RADIUS_PX
            || coords.svgX > margin.left + plotWidth + SNAP_RADIUS_PX
            || coords.svgY < margin.top - SNAP_RADIUS_PX
            || coords.svgY > margin.top + plotHeight + SNAP_RADIUS_PX
        ) {
            setHovered(null);
            return;
        }
        let bestItem = null;
        let bestDist2 = SNAP_RADIUS_SQ;
        for (let i = 0; i < plottedPoints.length; i += 1) {
            const item = plottedPoints[i];
            const dx = item.cx - coords.svgX;
            const dy = item.cy - coords.svgY;
            const d2 = dx * dx + dy * dy;
            if (d2 <= bestDist2) {
                bestDist2 = d2;
                bestItem = item;
            }
        }
        if (bestItem) {
            handlePointHover(bestItem.pt, bestItem.color, event);
        } else {
            setHovered(null);
        }
    };

    return (
        <ChartContainer title={title} subtitle={computedSubtitle} help={panelHelp} helpFormula={panelFormula} helpAlign="start">
            <div ref={containerRef} className="relative">
                <div className="w-full overflow-x-auto">
                    <svg
                        ref={svgRef}
                        viewBox={`0 0 ${svgWidth} ${height}`}
                        width="100%"
                        height={height}
                        className="block min-w-[640px] w-full select-none"
                        role="img"
                        aria-label={ariaLabel}
                        onMouseLeave={() => setHovered(null)}
                    >
                        <rect
                            x={margin.left}
                            y={margin.top}
                            width={plotWidth}
                            height={plotHeight}
                            rx={4}
                            fill="var(--bg-card)"
                            fillOpacity="0.45"
                            stroke="var(--border-color)"
                            strokeWidth="1"
                            onMouseMove={hasPoints ? handlePlotMouseMove : undefined}
                        />
                        {xTicks.map((tick, index) => (
                            <g key={`x-${index}`} pointerEvents="none">
                                <line
                                    x1={x(tick)}
                                    x2={x(tick)}
                                    y1={margin.top}
                                    y2={margin.top + plotHeight}
                                    stroke="var(--border-color)"
                                    strokeDasharray="3 3"
                                    strokeOpacity="0.7"
                                />
                                <text
                                    x={x(tick)}
                                    y={height - 24}
                                    textAnchor="middle"
                                    fill="var(--text-muted)"
                                    fontSize="10"
                                >
                                    {formatXTick(tick)}
                                </text>
                            </g>
                        ))}
                        {yTicks.map((tick, index) => (
                            <g key={`y-${index}`} pointerEvents="none">
                                <line
                                    x1={margin.left}
                                    x2={margin.left + plotWidth}
                                    y1={y(tick)}
                                    y2={y(tick)}
                                    stroke="var(--border-color)"
                                    strokeDasharray="3 3"
                                    strokeOpacity="0.7"
                                />
                                <text
                                    x={margin.left - 8}
                                    y={y(tick) + 3.5}
                                    textAnchor="end"
                                    fill="var(--text-muted)"
                                    fontSize="10"
                                >
                                    {formatYTick(tick)}
                                </text>
                            </g>
                        ))}
                        {hasPoints ? (
                            <>
                                {plottedPoints.map((item, index) => (
                                    <circle
                                        key={`${item.pt.t}-${item.pt.seq}-${index}`}
                                        cx={item.cx}
                                        cy={item.cy}
                                        r={dotRadius}
                                        fill={item.color}
                                        fillOpacity={0.8}
                                        className="cursor-pointer"
                                        onMouseEnter={(event) => {
                                            event?.stopPropagation?.();
                                            handlePointHover(item.pt, item.color, event);
                                        }}
                                        onMouseMove={(event) => {
                                            event?.stopPropagation?.();
                                            handlePointHover(item.pt, item.color, event);
                                        }}
                                    />
                                ))}
                                {activeHovered?.point && (
                                    <circle
                                        cx={x(activeHovered.point.t)}
                                        cy={y(activeHovered.point.seq)}
                                        r={dotRadius + 2}
                                        fill="none"
                                        stroke="var(--text-primary)"
                                        strokeWidth={1.75}
                                        pointerEvents="none"
                                    />
                                )}
                            </>
                        ) : (
                            cells.map((cell, index) => {
                                const normalized = metricRange > 0
                                    ? (Number(cell.average_metric_ms) - minimumMetric) / metricRange
                                    : 0;
                                const cellColor = heatmapColor(normalized);
                                const cellX = x(cell.start_seconds);
                                const cellY = y(cell.sequence_length_end);
                                const cellWidth = Math.max(1, x(cell.end_seconds) - cellX);
                                const cellHeight = Math.max(1, y(cell.sequence_length_start) - cellY);
                                const isHovered = activeHovered?.cell === cell;
                                return (
                                    <rect
                                        key={`${cell.start_seconds}-${cell.sequence_length_start}-${index}`}
                                        x={cellX}
                                        y={cellY}
                                        width={cellWidth}
                                        height={cellHeight}
                                        rx={1.5}
                                        ry={1.5}
                                        fill={cellColor}
                                        opacity={activeHovered ? (isHovered ? 1 : 0.8) : 1}
                                        stroke={isHovered ? 'var(--text-primary)' : 'var(--bg-card)'}
                                        strokeWidth={isHovered ? 1.5 : 0.75}
                                        className={cn('cursor-pointer transition-opacity duration-75')}
                                        onMouseEnter={(event) => handleCellHover(cell, cellColor, event)}
                                        onMouseMove={(event) => handleCellHover(cell, cellColor, event)}
                                        onMouseLeave={() => setHovered(null)}
                                    />
                                );
                            })
                        )}
                        <text
                            x={margin.left + plotWidth / 2}
                            y={height - 6}
                            textAnchor="middle"
                            fill="var(--text-muted)"
                            fontSize="10"
                            pointerEvents="none"
                        >
                            {xAxisLabel}
                        </text>
                        <text
                            transform={`translate(16 ${margin.top + plotHeight / 2}) rotate(-90)`}
                            textAnchor="middle"
                            fill="var(--text-muted)"
                            fontSize="10"
                            pointerEvents="none"
                        >
                            {`${sequenceLabel} (tokens)`}
                        </text>
                    </svg>
                </div>
                {activeHovered && (
                    <div
                        className="pointer-events-none absolute z-30"
                        style={{
                            left: `${Math.min(Math.max(8, activeHovered.xPx + 12), Math.max(8, (activeHovered.containerW || 640) - 248))}px`,
                            top: `${Math.max(8, Math.min(activeHovered.yPx - 16, Math.max(8, (activeHovered.containerH || 256) - 180)))}px`,
                        }}
                    >
                        {activeHovered.point ? (
                            <ChartTooltip title="Request">
                                <ChartTooltipRow
                                    label="Elapsed time"
                                    value={`${formatDecimal(activeHovered.point.t)} s`}
                                />
                                <ChartTooltipRow
                                    label={sequenceLabel}
                                    value={`${formatCount(Math.round(activeHovered.point.seq))} tok`}
                                />
                                <ChartTooltipRow
                                    color={activeHovered.color}
                                    label={metric}
                                    value={formatLatencyValue(activeHovered.point.ms)}
                                />
                            </ChartTooltip>
                        ) : (
                            <ChartTooltip title={`Time ${formatNumber(activeHovered.cell.start_seconds)}s – ${formatNumber(activeHovered.cell.end_seconds)}s`}>
                                <ChartTooltipRow
                                    label={sequenceLabel}
                                    value={`${formatNumber(activeHovered.cell.sequence_length_start)} – ${formatNumber(activeHovered.cell.sequence_length_end)} tok`}
                                />
                                <ChartTooltipRow
                                    color={activeHovered.color}
                                    label={`Avg ${metric}`}
                                    value={formatLatencyValue(activeHovered.cell.average_metric_ms)}
                                />
                                {Number.isFinite(activeHovered.cell.p50_ms) && (
                                    <ChartTooltipRow
                                        label={`P50 ${metric}`}
                                        value={formatLatencyValue(activeHovered.cell.p50_ms)}
                                    />
                                )}
                                {Number.isFinite(activeHovered.cell.p90_ms) && (
                                    <ChartTooltipRow
                                        label={`P90 ${metric}`}
                                        value={formatLatencyValue(activeHovered.cell.p90_ms)}
                                    />
                                )}
                                {Number.isFinite(activeHovered.cell.max_ms) && (
                                    <ChartTooltipRow
                                        label={`Max ${metric}`}
                                        value={formatLatencyValue(activeHovered.cell.max_ms)}
                                    />
                                )}
                                <ChartTooltipRow
                                    label="Requests"
                                    value={formatCount(activeHovered.cell.request_count)}
                                />
                            </ChartTooltip>
                        )}
                    </div>
                )}
            </div>
            <div className="mt-3 flex flex-wrap items-center justify-between gap-3 text-[11px] text-theme-muted">
                <span className="font-mono">{summaryText}</span>
                <div className="flex flex-wrap items-center gap-2" aria-label={`${colorLegendPrefix} scale`}>
                    <span className="font-medium text-theme-text">{colorLegendPrefix}</span>
                    {isUniformMetric ? (
                        <>
                            <span
                                className="h-2.5 w-5 rounded-full border border-theme-border/60"
                                style={{ background: heatmapColor(0) }}
                                aria-hidden="true"
                            />
                            <span className="font-mono">{`Uniform (${formatLatencyValue(minimumMetric)})`}</span>
                        </>
                    ) : (
                        <>
                            <span className="font-mono">{`Low (${formatLatencyValue(minimumMetric)})`}</span>
                            <span
                                className="relative inline-flex h-2.5 w-28 overflow-hidden rounded-full border border-theme-border/60"
                                aria-hidden="true"
                            >
                                <span className="h-full w-full" style={{ background: HEATMAP_GRADIENT }} />
                                <span className="absolute inset-y-0 left-1/3 w-px bg-theme-card/60" />
                                <span className="absolute inset-y-0 left-2/3 w-px bg-theme-card/60" />
                            </span>
                            <span className="font-mono">{`High (${formatLatencyValue(maximumMetric)})`}</span>
                        </>
                    )}
                </div>
            </div>
        </ChartContainer>
    );
}
