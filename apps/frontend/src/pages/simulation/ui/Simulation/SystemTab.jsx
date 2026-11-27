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

import React, { useMemo, useState } from 'react';
import { Cpu, HardDrive, Network, Server, Thermometer, Zap } from 'lucide-react';
import {
    CartesianGrid,
    Line,
    LineChart,
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
    Select,
    StatCard,
    ToggleGroup,
} from '../../../../shared/ui';
import {
    clampPercent,
    displayName,
    formatBytePair,
    formatByteRatePair,
    formatBytes,
    formatCount,
    formatDecimal,
    formatEnergyWh,
    formatNumber,
    formatWatts,
    numberAt,
    selectByteRateUnitScale,
    selectByteUnitScale,
} from '../../model/presentation';

const AXIS_TICK = { fill: 'var(--text-muted)', fontSize: 10 };
const LINE_TOOLTIP_CURSOR = { stroke: 'var(--chart-cursor-stroke, rgba(148, 163, 184, 0.28))' };
const BYTES_PER_MIB = 1024 ** 2;
const BYTES_PER_GIB = 1024 ** 3;
const ALL_NODES_SCOPE = '__all__';
const ALL_NICS_SCOPE = '__all__';
const BY_NIC_SCOPE = '__by_nic__';
const MAX_BY_NIC_SLOTS = CHART_SERIES.length;
const EMPTY_OBJ = {};
const EMPTY_ARR = [];

function hasSeriesValue(pt, key) {
    return pt?.[key] !== null && pt?.[key] !== undefined;
}

function firstFinite(...values) {
    for (const value of values) {
        if (value !== null && value !== undefined && value !== '' && typeof value !== 'boolean') {
            const numeric = Number(value);
            if (Number.isFinite(numeric)) return numeric;
        }
    }
    return null;
}

function legacyGbToBytes(gb) {
    const numeric = firstFinite(gb);
    return numeric !== null ? numeric * BYTES_PER_GIB : null;
}

function legacyMbpsToBps(mbps) {
    const numeric = firstFinite(mbps);
    return numeric !== null ? numeric * BYTES_PER_MIB : null;
}

function sumNicMapRates(nics) {
    if (!nics) return { rxBps: null, txBps: null };
    let rxSum = 0;
    let txSum = 0;
    let anyRx = false;
    let anyTx = false;
    for (const entry of Object.values(nics)) {
        if (entry?.rx_bps !== null && entry?.rx_bps !== undefined) {
            rxSum += entry.rx_bps;
            anyRx = true;
        }
        if (entry?.tx_bps !== null && entry?.tx_bps !== undefined) {
            txSum += entry.tx_bps;
            anyTx = true;
        }
    }
    return {
        rxBps: anyRx ? rxSum : null,
        txBps: anyTx ? txSum : null,
    };
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

function formatAxisTick(value) {
    if (value === null || value === undefined || value === '' || typeof value === 'boolean') return '';
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return '';
    return formatDecimal(numeric);
}

function UnitTooltip({ active, payload, label, unit }) {
    if (!active || !payload?.length) return null;
    const rows = payload.filter((item) => item.value !== null && item.value !== undefined);
    if (!rows.length) return null;
    return (
        <ChartTooltip title={`Elapsed ${formatNumber(label)} s`}>
            {rows.map((item) => (
                <ChartTooltipRow
                    key={item.dataKey}
                    color={item.color}
                    label={item.name}
                    value={formatDecimal(item.value)}
                    unit={` ${unit}`}
                />
            ))}
        </ChartTooltip>
    );
}

function normalizeSeriesPoint(pt, idx, originMs, { allowBareNodeFields = false } = {}) {
    const parsedTs = pt?.timestamp ? Date.parse(pt.timestamp) : NaN;
    const relSeconds = originMs !== null && Number.isFinite(parsedTs)
        ? Math.max(0, (parsedTs - originMs) / 1000)
        : idx;

    const memAvailBytes = firstFinite(
        pt?.host_memory_available_bytes,
        allowBareNodeFields ? pt?.memory_available_bytes : null,
        legacyGbToBytes(pt?.host_memory_available_gb),
        legacyGbToBytes(pt?.node_memory_available_gb),
    );
    const memUsedBytes = firstFinite(
        pt?.host_memory_used_bytes,
        allowBareNodeFields ? pt?.memory_used_bytes : null,
        legacyGbToBytes(pt?.host_memory_used_gb),
        legacyGbToBytes(pt?.node_memory_used_gb),
    );
    const computedMemPct = (memUsedBytes !== null && memAvailBytes !== null && (memUsedBytes + memAvailBytes) > 0)
        ? (memUsedBytes / (memUsedBytes + memAvailBytes)) * 100
        : null;

    let nics = null;
    const rawNics = pt?.network_interfaces;
    if (rawNics && typeof rawNics === 'object' && !Array.isArray(rawNics)) {
        for (const [rawDevice, stats] of Object.entries(rawNics)) {
            const device = typeof rawDevice === 'string' ? rawDevice.trim() : '';
            if (!device || !stats || typeof stats !== 'object') continue;
            const rxBps = firstFinite(stats.rx, stats.rx_bytes_per_second);
            const txBps = firstFinite(stats.tx, stats.tx_bytes_per_second);
            if (rxBps === null && txBps === null) continue;
            if (!nics) nics = Object.create(null);
            nics[device] = { rx_bps: rxBps, tx_bps: txBps };
        }
    }

    let netRxBps = firstFinite(
        pt?.host_network_receive_bytes_per_second,
        pt?.network_receive_bytes_per_second,
    );
    let netTxBps = firstFinite(
        pt?.host_network_transmit_bytes_per_second,
        pt?.network_transmit_bytes_per_second,
    );
    if (nics && (netRxBps === null || netTxBps === null)) {
        const summed = sumNicMapRates(nics);
        if (netRxBps === null) netRxBps = summed.rxBps;
        if (netTxBps === null) netTxBps = summed.txBps;
    }

    return {
        t: firstFinite(pt?.elapsed_seconds, pt?.time_seconds, pt?.t, relSeconds),
        cpu_pct: clampPercent(firstFinite(
            pt?.host_cpu_usage_percent,
            pt?.host_cpu_percent,
            pt?.node_cpu_percent,
            allowBareNodeFields ? pt?.cpu_usage_percent : null,
            pt?.cpu_percent,
        )),
        mem_avail_bytes: memAvailBytes,
        mem_used_bytes: memUsedBytes,
        mem_pct: clampPercent(firstFinite(
            pt?.host_memory_usage_percent,
            pt?.node_memory_usage_percent,
            allowBareNodeFields ? pt?.memory_usage_percent : null,
            computedMemPct,
        )),
        disk_read_bps: firstFinite(
            pt?.host_disk_read_bytes_per_second,
            allowBareNodeFields ? pt?.disk_read_bytes_per_second : null,
            legacyMbpsToBps(pt?.host_disk_read_mbps),
            legacyMbpsToBps(pt?.node_disk_read_mbps),
        ),
        disk_write_bps: firstFinite(
            pt?.host_disk_written_bytes_per_second,
            pt?.host_disk_write_bytes_per_second,
            allowBareNodeFields ? pt?.disk_written_bytes_per_second : null,
            legacyMbpsToBps(pt?.host_disk_write_mbps),
            legacyMbpsToBps(pt?.node_disk_write_mbps),
        ),
        net_rx_bps: netRxBps,
        net_tx_bps: netTxBps,
        nics,
        host_power_w: firstFinite(pt?.host_power_watts, pt?.node_power_watts),
        accel_power_w: firstFinite(pt?.gpu_power_watts, pt?.power_watts, pt?.accelerator_power_watts),
    };
}

function buildNetworkPlot({ activeNic, availableNics, activeSeries, scopeLabel, isAllNodes, activeNode }) {
    if (activeNic === BY_NIC_SCOPE) {
        const capped = availableNics.length > MAX_BY_NIC_SLOTS;
        const rawSlots = capped
            ? [
                ...availableNics.slice(0, MAX_BY_NIC_SLOTS - 1).map((dev, idx) => ({
                    id: idx,
                    label: dev,
                    devices: [dev],
                    color: CHART_SERIES[idx],
                })),
                {
                    id: MAX_BY_NIC_SLOTS - 1,
                    label: 'Other',
                    devices: availableNics.slice(MAX_BY_NIC_SLOTS - 1),
                    color: CHART_SERIES[MAX_BY_NIC_SLOTS - 1],
                },
            ]
            : availableNics.map((dev, idx) => ({
                id: idx,
                label: dev,
                devices: [dev],
                color: CHART_SERIES[idx],
            }));

        const rawData = activeSeries.map((pt) => {
            const row = { t: pt.t };
            for (const slot of rawSlots) {
                let rxSum = 0;
                let txSum = 0;
                let anyRx = false;
                let anyTx = false;
                for (const dev of slot.devices) {
                    const entry = (pt.nics && Object.prototype.hasOwnProperty.call(pt.nics, dev))
                        ? pt.nics[dev]
                        : null;
                    if (entry?.rx_bps !== null && entry?.rx_bps !== undefined) {
                        rxSum += entry.rx_bps;
                        anyRx = true;
                    }
                    if (entry?.tx_bps !== null && entry?.tx_bps !== undefined) {
                        txSum += entry.tx_bps;
                        anyTx = true;
                    }
                }
                row[`nic_${slot.id}_rx`] = anyRx ? rxSum : null;
                row[`nic_${slot.id}_tx`] = anyTx ? txSum : null;
            }
            return row;
        });

        const slots = rawSlots
            .map((slot) => {
                const rxKey = `nic_${slot.id}_rx`;
                const txKey = `nic_${slot.id}_tx`;
                const hasRx = rawData.some((row) => hasSeriesValue(row, rxKey));
                const hasTx = rawData.some((row) => hasSeriesValue(row, txKey));
                return { ...slot, rxKey, txKey, hasRx, hasTx };
            })
            .filter((slot) => slot.hasRx || slot.hasTx);

        const plottedValues = [];
        for (const row of rawData) {
            for (const slot of slots) {
                if (slot.hasRx && hasSeriesValue(row, slot.rxKey)) plottedValues.push(row[slot.rxKey]);
                if (slot.hasTx && hasSeriesValue(row, slot.txKey)) plottedValues.push(row[slot.txKey]);
            }
        }
        const scale = selectByteRateUnitScale(plottedValues);
        const data = rawData.map((row) => {
            const scaledRow = { t: row.t };
            for (const slot of slots) {
                if (slot.hasRx) {
                    scaledRow[slot.rxKey] = hasSeriesValue(row, slot.rxKey) ? row[slot.rxKey] / scale.divisor : null;
                }
                if (slot.hasTx) {
                    scaledRow[slot.txKey] = hasSeriesValue(row, slot.txKey) ? row[slot.txKey] / scale.divisor : null;
                }
            }
            return scaledRow;
        });
        const hasData = slots.length > 0;
        const unitSuffix = hasData ? ` (${scale.unit})` : '';

        return {
            mode: 'by_nic',
            data,
            slots,
            hasData,
            unit: scale.unit,
            subtitle: capped
                ? `Top 4 interfaces + Other RX (solid) and TX (dashed) throughput${unitSuffix} · ${scopeLabel}`
                : `Per-interface RX (solid) and TX (dashed) throughput${unitSuffix} · ${scopeLabel}`,
        };
    }

    if (activeNic !== ALL_NICS_SCOPE) {
        const rawData = activeSeries.map((pt) => {
            const entry = (pt.nics && Object.prototype.hasOwnProperty.call(pt.nics, activeNic))
                ? pt.nics[activeNic]
                : null;
            return {
                t: pt.t,
                sel_nic_rx: entry?.rx_bps ?? null,
                sel_nic_tx: entry?.tx_bps ?? null,
            };
        });
        const hasRx = rawData.some((row) => hasSeriesValue(row, 'sel_nic_rx'));
        const hasTx = rawData.some((row) => hasSeriesValue(row, 'sel_nic_tx'));
        const plottedValues = [];
        for (const row of rawData) {
            if (hasSeriesValue(row, 'sel_nic_rx')) plottedValues.push(row.sel_nic_rx);
            if (hasSeriesValue(row, 'sel_nic_tx')) plottedValues.push(row.sel_nic_tx);
        }
        const scale = selectByteRateUnitScale(plottedValues);
        const data = rawData.map((row) => ({
            t: row.t,
            sel_nic_rx: hasSeriesValue(row, 'sel_nic_rx') ? row.sel_nic_rx / scale.divisor : null,
            sel_nic_tx: hasSeriesValue(row, 'sel_nic_tx') ? row.sel_nic_tx / scale.divisor : null,
        }));
        const hasData = hasRx || hasTx;
        const unitSuffix = hasData ? ` (${scale.unit})` : '';
        return {
            mode: 'single_nic',
            data,
            hasRx,
            hasTx,
            hasData,
            unit: scale.unit,
            subtitle: `Interface ${activeNic} RX / TX throughput${unitSuffix} · ${scopeLabel}`,
        };
    }

    const hasRx = activeSeries.some((pt) => hasSeriesValue(pt, 'net_rx_bps'));
    const hasTx = activeSeries.some((pt) => hasSeriesValue(pt, 'net_tx_bps'));
    const plottedValues = [];
    for (const pt of activeSeries) {
        if (hasSeriesValue(pt, 'net_rx_bps')) plottedValues.push(pt.net_rx_bps);
        if (hasSeriesValue(pt, 'net_tx_bps')) plottedValues.push(pt.net_tx_bps);
    }
    const scale = selectByteRateUnitScale(plottedValues);
    const data = activeSeries.map((pt) => ({
        t: pt.t,
        net_rx: hasSeriesValue(pt, 'net_rx_bps') ? pt.net_rx_bps / scale.divisor : null,
        net_tx: hasSeriesValue(pt, 'net_tx_bps') ? pt.net_tx_bps / scale.divisor : null,
    }));
    const hasData = hasRx || hasTx;
    const unitSuffix = hasData ? ` (${scale.unit})` : '';
    return {
        mode: 'all',
        data,
        hasRx,
        hasTx,
        hasData,
        unit: scale.unit,
        subtitle: isAllNodes
            ? `Host network receive and transmit throughput${unitSuffix}`
            : `Network RX / TX throughput${unitSuffix} on ${activeNode}`,
    };
}

function roleBadgeTone(role) {
    const normalized = String(role || '').toLowerCase();
    if (normalized === 'prefill') return 'info';
    if (normalized === 'decode') return 'violet';
    if (normalized === 'epp' || normalized === 'router') return 'brand';
    return 'neutral';
}

export function SystemTab({ telemetry, initialNode = ALL_NODES_SCOPE, initialNic = ALL_NICS_SCOPE }) {
    const [selectedNode, setSelectedNode] = useState(initialNode);
    const [selectedNic, setSelectedNic] = useState(initialNic);
    const [ioView, setIoView] = useState(null);

    const serverTelemetry = telemetry?.server_telemetry ?? EMPTY_OBJ;
    const host = serverTelemetry?.host_metrics ?? serverTelemetry?.system ?? serverTelemetry?.node_metrics ?? EMPTY_OBJ;
    const summary = serverTelemetry?.summary ?? EMPTY_OBJ;
    const hwProfile = serverTelemetry?.hardware_profile ?? serverTelemetry?.hardware_profiles?.[0] ?? null;
    const accelLabel = hwProfile?.ui?.labels?.metric_group
        || hwProfile?.metric_group
        || hwProfile?.display_name
        || 'Accelerator';

    const perNodeRows = Array.isArray(serverTelemetry?.per_node) ? serverTelemetry.per_node : EMPTY_ARR;
    const rawPerNodeSeries = (serverTelemetry?.per_node_series && typeof serverTelemetry.per_node_series === 'object')
        ? serverTelemetry.per_node_series
        : EMPTY_OBJ;

    const nodeList = useMemo(() => {
        const seen = new Set();
        const result = [];
        const pushNode = (name) => {
            if (typeof name === 'string' && name.trim() && !seen.has(name)) {
                seen.add(name);
                result.push(name);
            }
        };
        for (const row of perNodeRows) pushNode(row?.node);
        if (Array.isArray(host?.nodes)) {
            for (const node of host.nodes) pushNode(node);
        }
        for (const node of Object.keys(rawPerNodeSeries)) pushNode(node);
        return result;
    }, [perNodeRows, host, rawPerNodeSeries]);

    const activeNode = (selectedNode !== ALL_NODES_SCOPE && nodeList.includes(selectedNode))
        ? selectedNode
        : ALL_NODES_SCOPE;
    const isAllNodes = activeNode === ALL_NODES_SCOPE;

    const originEpoch = telemetry?.client_series?.origin_epoch;
    const executionStartedAt = telemetry?.run_manifest?.execution_started_at;

    const { hostSeries, perNodeSeriesByNode } = useMemo(() => {
        const rawGlobal = Array.isArray(host?.series)
            ? host.series
            : Array.isArray(serverTelemetry?.series)
                ? serverTelemetry.series
                : EMPTY_ARR;

        let firstPerNodeTs = null;
        for (const pts of Object.values(rawPerNodeSeries)) {
            if (Array.isArray(pts) && pts[0]?.timestamp) {
                const parsed = Date.parse(pts[0].timestamp);
                if (Number.isFinite(parsed)) {
                    firstPerNodeTs = parsed;
                    break;
                }
            }
        }

        const originCandidates = [
            originEpoch ? originEpoch * 1000 : null,
            serverTelemetry?.window?.start ? Date.parse(serverTelemetry.window.start) : null,
            executionStartedAt ? Date.parse(executionStartedAt) : null,
            rawGlobal[0]?.timestamp ? Date.parse(rawGlobal[0].timestamp) : null,
            firstPerNodeTs,
        ];
        const originMs = originCandidates.find((v) => typeof v === 'number' && Number.isFinite(v)) ?? null;

        const normalizedGlobal = rawGlobal.map((pt, idx) => normalizeSeriesPoint(pt, idx, originMs));
        const byNode = Object.create(null);
        for (const [nodeName, pts] of Object.entries(rawPerNodeSeries)) {
            if (!Object.prototype.hasOwnProperty.call(rawPerNodeSeries, nodeName) || !Array.isArray(pts)) continue;
            byNode[nodeName] = pts.map((pt, idx) => normalizeSeriesPoint(pt, idx, originMs, { allowBareNodeFields: true }));
        }
        if (normalizedGlobal.length > 0 && !normalizedGlobal.some((pt) => pt.nics)) {
            const keyFor = (t) => Math.round(Number(t) * 100) / 100;
            const nicsByTime = new Map();
            for (const pts of Object.values(byNode)) {
                for (const pt of pts) {
                    if (pt.t === null || !pt.nics) continue;
                    const timeKey = keyFor(pt.t);
                    let bucket = nicsByTime.get(timeKey);
                    if (!bucket) {
                        bucket = Object.create(null);
                        nicsByTime.set(timeKey, bucket);
                    }
                    for (const [device, vals] of Object.entries(pt.nics)) {
                        if (!Object.prototype.hasOwnProperty.call(bucket, device)) {
                            bucket[device] = { rx_bps: null, tx_bps: null };
                        }
                        if (vals.rx_bps !== null) {
                            bucket[device].rx_bps = (bucket[device].rx_bps ?? 0) + vals.rx_bps;
                        }
                        if (vals.tx_bps !== null) {
                            bucket[device].tx_bps = (bucket[device].tx_bps ?? 0) + vals.tx_bps;
                        }
                    }
                }
            }
            if (nicsByTime.size > 0) {
                for (const pt of normalizedGlobal) {
                    if (pt.t === null) continue;
                    const bucket = nicsByTime.get(keyFor(pt.t));
                    if (bucket) {
                        pt.nics = bucket;
                        if (pt.net_rx_bps === null || pt.net_tx_bps === null) {
                            const summed = sumNicMapRates(bucket);
                            if (pt.net_rx_bps === null) pt.net_rx_bps = summed.rxBps;
                            if (pt.net_tx_bps === null) pt.net_tx_bps = summed.txBps;
                        }
                    }
                }
            }
        }
        return { hostSeries: normalizedGlobal, perNodeSeriesByNode: byNode };
    }, [host, serverTelemetry, rawPerNodeSeries, originEpoch, executionStartedAt]);

    // Global aggregate KPI values (always computed for availability check and All-Nodes scope).
    const globalCpuMean = clampPercent(firstFinite(
        host?.cpu_avg_pct,
        host?.cpu_percent?.mean,
        host?.cpu_pct,
        numberAt(summary, ['host_cpu_usage_percent.mean', 'host_cpu_percent.mean']),
    ));
    const globalCpuPeak = clampPercent(firstFinite(
        host?.cpu_peak_pct,
        host?.cpu_percent?.max,
        numberAt(summary, ['host_cpu_usage_percent.max', 'host_cpu_percent.max']),
    ));
    const globalMemAvailMeanBytes = firstFinite(
        host?.mem_avail_bytes,
        numberAt(summary, ['host_memory_available_bytes.mean']),
        legacyGbToBytes(host?.memory_available_gb?.mean),
        legacyGbToBytes(host?.memory_available_gb),
        legacyGbToBytes(numberAt(summary, ['host_memory_available_gb.mean'])),
    );
    const globalMemAvailMinBytes = firstFinite(
        host?.mem_avail_min_bytes,
        numberAt(summary, ['host_memory_available_bytes.min']),
        legacyGbToBytes(host?.memory_available_gb?.min),
        globalMemAvailMeanBytes,
    );
    const globalMemTotalBytes = firstFinite(
        host?.mem_total_bytes,
        numberAt(summary, ['host_memory_total_bytes.mean']),
        legacyGbToBytes(host?.memory_total_gb),
        legacyGbToBytes(numberAt(summary, ['host_memory_total_gb.mean'])),
    );
    const globalMemUsedBytes = firstFinite(
        host?.mem_used_bytes,
        numberAt(summary, ['host_memory_used_bytes.mean']),
        legacyGbToBytes(host?.memory_used_gb?.mean),
        legacyGbToBytes(host?.memory_used_gb),
        (globalMemTotalBytes !== null && globalMemAvailMeanBytes !== null)
            ? Math.max(0, globalMemTotalBytes - globalMemAvailMeanBytes)
            : null,
    );
    const globalMemPeakPct = clampPercent(firstFinite(
        host?.mem_peak_pct,
        host?.memory_usage_percent?.max,
        numberAt(summary, ['host_memory_usage_percent.max']),
        (globalMemTotalBytes !== null && globalMemTotalBytes > 0 && globalMemAvailMinBytes !== null)
            ? ((globalMemTotalBytes - globalMemAvailMinBytes) / globalMemTotalBytes) * 100
            : null,
    ));
    const globalDiskFreeBytes = firstFinite(
        host?.disk_avail_bytes,
        numberAt(summary, ['host_filesystem_avail_bytes.mean', 'host_filesystem_avail_bytes.min']),
        legacyGbToBytes(host?.disk_free_gb?.min),
        legacyGbToBytes(host?.disk_free_gb),
        legacyGbToBytes(numberAt(summary, ['host_disk_free_gb.min', 'host_disk_free_gb.mean'])),
    );
    const globalDiskTotalBytes = firstFinite(
        host?.disk_total_bytes,
        numberAt(summary, ['host_filesystem_size_bytes.mean', 'host_filesystem_size_bytes.max']),
        legacyGbToBytes(host?.disk_total_gb),
    );
    const globalDiskReadBps = firstFinite(
        host?.disk_read_bps,
        numberAt(summary, ['host_disk_read_bytes_per_second.mean']),
        legacyMbpsToBps(host?.disk_read_mbps?.mean),
        legacyMbpsToBps(host?.disk_read_mbps),
    );
    const globalDiskWriteBps = firstFinite(
        host?.disk_write_bps,
        numberAt(summary, ['host_disk_written_bytes_per_second.mean']),
        legacyMbpsToBps(host?.disk_write_mbps?.mean),
        legacyMbpsToBps(host?.disk_write_mbps),
    );
    const globalNetRxBps = firstFinite(
        host?.net_rx_bps,
        numberAt(summary, ['host_network_receive_bytes_per_second.mean', 'network_receive_bytes_per_second.mean']),
    );
    const globalNetTxBps = firstFinite(
        host?.net_tx_bps,
        numberAt(summary, ['host_network_transmit_bytes_per_second.mean', 'network_transmit_bytes_per_second.mean']),
    );
    const globalNetRxPeakBps = firstFinite(
        host?.net_rx_peak_bps,
        numberAt(summary, ['host_network_receive_bytes_per_second.max', 'network_receive_bytes_per_second.max']),
    );
    const globalNetTxPeakBps = firstFinite(
        host?.net_tx_peak_bps,
        numberAt(summary, ['host_network_transmit_bytes_per_second.max', 'network_transmit_bytes_per_second.max']),
    );
    const globalHostPowerMean = firstFinite(
        host?.power_watts?.mean,
        host?.host_power_watts,
        serverTelemetry?.energy?.host_avg_power_watts,
        numberAt(summary, ['host_power_watts.mean']),
    );
    const globalHostPowerPeak = firstFinite(
        host?.power_watts?.max,
        host?.host_peak_power_watts,
        serverTelemetry?.energy?.host_peak_power_watts,
        numberAt(summary, ['host_power_watts.max']),
    );
    const globalHostEnergyWh = firstFinite(
        serverTelemetry?.energy?.host_energy_wh,
        host?.host_energy_wh,
    );
    const globalAccelPowerMean = firstFinite(
        serverTelemetry?.energy?.accelerator_avg_power_watts,
        numberAt(summary, ['gpu_power_watts.mean', 'power_watts.mean', 'accelerator_power_watts.mean']),
        serverTelemetry?.power?.avg_watts,
    );
    const globalAccelPowerPeak = firstFinite(
        serverTelemetry?.energy?.accelerator_peak_power_watts,
        numberAt(summary, ['gpu_power_watts.max', 'power_watts.max', 'accelerator_power_watts.max']),
        serverTelemetry?.power?.peak_watts,
    );
    const globalAccelTempPeak = firstFinite(
        numberAt(summary, [
            'gpu_temperature_celsius.max',
            'accelerator_temperature_celsius.max',
            'temperature_celsius.max',
            'gpu_temperature_celsius.mean',
        ]),
    );

    const hasGlobalSeriesMetric = (key) => hostSeries.some((pt) => hasSeriesValue(pt, key))
        || Object.values(perNodeSeriesByNode).some((pts) => pts.some((pt) => hasSeriesValue(pt, key)));

    const hasAnyHostMetric = globalCpuMean !== null
        || globalCpuPeak !== null
        || globalMemAvailMeanBytes !== null
        || globalMemUsedBytes !== null
        || globalMemTotalBytes !== null
        || globalMemPeakPct !== null
        || globalDiskFreeBytes !== null
        || globalDiskTotalBytes !== null
        || globalDiskReadBps !== null
        || globalDiskWriteBps !== null
        || globalNetRxBps !== null
        || globalNetTxBps !== null
        || globalHostPowerMean !== null
        || globalHostPowerPeak !== null
        || globalHostEnergyWh !== null
        || nodeList.length > 0
        || perNodeRows.length > 0
        || hasGlobalSeriesMetric('cpu_pct')
        || hasGlobalSeriesMetric('mem_avail_bytes')
        || hasGlobalSeriesMetric('mem_used_bytes')
        || hasGlobalSeriesMetric('mem_pct')
        || hasGlobalSeriesMetric('disk_read_bps')
        || hasGlobalSeriesMetric('disk_write_bps')
        || hasGlobalSeriesMetric('net_rx_bps')
        || hasGlobalSeriesMetric('net_tx_bps')
        || hasGlobalSeriesMetric('host_power_w');

    const hasGlobalAccelPower = globalAccelPowerMean !== null || hasGlobalSeriesMetric('accel_power_w');
    const isServerAvailable = serverTelemetry?.status === 'available'
        || (serverTelemetry?.available === true && serverTelemetry?.status !== 'unavailable');

    // Per-node multi-line overlay metadata for All Nodes mode (bounded to 4 overlays so palette stays within CHART_SERIES[0..4]).
    const overlayNodes = useMemo(() => {
        if (!isAllNodes) return EMPTY_ARR;
        const nodesWithSeries = nodeList.filter((name) => (
            Object.prototype.hasOwnProperty.call(perNodeSeriesByNode, name)
            && perNodeSeriesByNode[name].length > 0
        ));
        if (nodesWithSeries.length < 2) return EMPTY_ARR;
        return nodesWithSeries.slice(0, CHART_SERIES.length - 1).map((node, idx) => ({
            node,
            color: CHART_SERIES[idx + 1],
            cpuKey: `node_cpu_${idx}`,
            memPctKey: `node_mem_pct_${idx}`,
            series: perNodeSeriesByNode[node],
        }));
    }, [isAllNodes, nodeList, perNodeSeriesByNode]);

    const activeSeries = useMemo(
        () => (
            isAllNodes
                ? hostSeries
                : (Object.prototype.hasOwnProperty.call(perNodeSeriesByNode, activeNode)
                    ? perNodeSeriesByNode[activeNode]
                    : EMPTY_ARR)
        ),
        [isAllNodes, hostSeries, perNodeSeriesByNode, activeNode],
    );

    const overlayMergedSeries = useMemo(() => {
        if (!isAllNodes || overlayNodes.length < 2) return activeSeries;
        const byTime = new Map();
        const keyFor = (t) => Math.round(Number(t) * 100) / 100;
        const ensureRow = (t) => {
            const k = keyFor(t);
            let row = byTime.get(k);
            if (!row) {
                row = { t: Number(t), cpu_pct: null, mem_pct: null };
                byTime.set(k, row);
            }
            return row;
        };
        for (const pt of hostSeries) {
            if (pt.t === null) continue;
            const row = ensureRow(pt.t);
            row.cpu_pct = pt.cpu_pct;
            row.mem_pct = pt.mem_pct;
        }
        for (const nodeInfo of overlayNodes) {
            for (const pt of nodeInfo.series) {
                if (pt.t === null) continue;
                const row = ensureRow(pt.t);
                row[nodeInfo.cpuKey] = pt.cpu_pct;
                row[nodeInfo.memPctKey] = pt.mem_pct;
            }
        }
        return Array.from(byTime.values()).sort((a, b) => a.t - b.t);
    }, [isAllNodes, overlayNodes, activeSeries, hostSeries]);

    const scopeLabel = isAllNodes ? 'All Nodes (Aggregate)' : activeNode;
    const selectedRow = !isAllNodes ? perNodeRows.find((r) => r?.node === activeNode) ?? null : null;

    const nodeScopeOptions = useMemo(() => [
        { value: ALL_NODES_SCOPE, label: 'All Nodes (Aggregate)' },
        ...nodeList.map((node) => ({ value: node, label: node })),
    ], [nodeList]);

    const summaryNics = useMemo(() => {
        if (isAllNodes) {
            return Array.isArray(host?.network_interfaces) ? host.network_interfaces : EMPTY_ARR;
        }
        return Array.isArray(selectedRow?.network_interfaces) ? selectedRow.network_interfaces : EMPTY_ARR;
    }, [isAllNodes, host, selectedRow]);

    const availableNics = useMemo(() => {
        const byDevice = new Map();
        for (const entry of summaryNics) {
            const device = typeof entry?.device === 'string' ? entry.device.trim() : '';
            if (!device || byDevice.has(device)) continue;
            const meanRx = firstFinite(entry?.rx_bytes_per_second?.mean) ?? 0;
            const meanTx = firstFinite(entry?.tx_bytes_per_second?.mean) ?? 0;
            const maxRx = firstFinite(entry?.rx_bytes_per_second?.max) ?? 0;
            const maxTx = firstFinite(entry?.tx_bytes_per_second?.max) ?? 0;
            byDevice.set(device, {
                device,
                meanTotal: meanRx + meanTx,
                maxTotal: maxRx + maxTx,
            });
        }
        const seriesStats = new Map();
        for (const pt of activeSeries) {
            if (!pt?.nics) continue;
            for (const [rawDevice, vals] of Object.entries(pt.nics)) {
                const device = rawDevice.trim();
                if (!device || byDevice.has(device)) continue;
                const totalBps = (vals?.rx_bps ?? 0) + (vals?.tx_bps ?? 0);
                const prev = seriesStats.get(device) || { sum: 0, count: 0, max: 0 };
                prev.sum += totalBps;
                prev.count += 1;
                if (totalBps > prev.max) prev.max = totalBps;
                seriesStats.set(device, prev);
            }
        }
        for (const [device, st] of seriesStats.entries()) {
            const meanBps = st.count > 0 ? st.sum / st.count : 0;
            byDevice.set(device, {
                device,
                meanTotal: meanBps,
                maxTotal: st.max,
            });
        }
        return Array.from(byDevice.values())
            .sort((a, b) => (
                (b.meanTotal - a.meanTotal)
                || (b.maxTotal - a.maxTotal)
                || (a.device < b.device ? -1 : a.device > b.device ? 1 : 0)
            ))
            .map((item) => item.device);
    }, [summaryNics, activeSeries]);

    const activeNic = (
        selectedNic === ALL_NICS_SCOPE
        || (selectedNic === BY_NIC_SCOPE && availableNics.length >= 2)
        || availableNics.includes(selectedNic)
    )
        ? selectedNic
        : ALL_NICS_SCOPE;

    const nicOptions = useMemo(() => {
        if (availableNics.length === 0) return EMPTY_ARR;
        return [
            { value: ALL_NICS_SCOPE, label: 'All NICs (Total)' },
            ...(availableNics.length >= 2 ? [{ value: BY_NIC_SCOPE, label: 'By NIC' }] : []),
            ...availableNics.map((device) => ({ value: device, label: device })),
        ];
    }, [availableNics]);

    const { memScale, memChartData } = useMemo(() => {
        const byteValues = [];
        for (const pt of activeSeries) {
            if (hasSeriesValue(pt, 'mem_used_bytes')) byteValues.push(pt.mem_used_bytes);
            if (hasSeriesValue(pt, 'mem_avail_bytes')) byteValues.push(pt.mem_avail_bytes);
        }
        const scale = selectByteUnitScale(byteValues);
        const data = activeSeries.map((pt) => ({
            t: pt.t,
            mem_used: hasSeriesValue(pt, 'mem_used_bytes') ? pt.mem_used_bytes / scale.divisor : null,
            mem_avail: hasSeriesValue(pt, 'mem_avail_bytes') ? pt.mem_avail_bytes / scale.divisor : null,
        }));
        return { memScale: scale, memChartData: data };
    }, [activeSeries]);

    const { diskScale, diskChartData } = useMemo(() => {
        const bpsValues = [];
        for (const pt of activeSeries) {
            if (hasSeriesValue(pt, 'disk_read_bps')) bpsValues.push(pt.disk_read_bps);
            if (hasSeriesValue(pt, 'disk_write_bps')) bpsValues.push(pt.disk_write_bps);
        }
        const scale = selectByteRateUnitScale(bpsValues);
        const data = activeSeries.map((pt) => ({
            t: pt.t,
            disk_read: hasSeriesValue(pt, 'disk_read_bps') ? pt.disk_read_bps / scale.divisor : null,
            disk_write: hasSeriesValue(pt, 'disk_write_bps') ? pt.disk_write_bps / scale.divisor : null,
        }));
        return { diskScale: scale, diskChartData: data };
    }, [activeSeries]);

    const netPlot = useMemo(() => buildNetworkPlot({
        activeNic,
        availableNics,
        activeSeries,
        scopeLabel,
        isAllNodes,
        activeNode,
    }), [activeNic, availableNics, activeSeries, scopeLabel, isAllNodes, activeNode]);

    if (!isServerAvailable || (!hasAnyHostMetric && !hasGlobalAccelPower)) {
        return (
            <Panel>
                <EmptyState
                    icon={<Server className="h-7 w-7" />}
                    title="Host system metrics unavailable"
                    message={
                        host?.reason
                        || serverTelemetry?.reason
                        || 'Node-level host metrics require Prometheus with node_exporter and node-scoped visibility (unavailable on external endpoints or restricted Non-Admin clusters).'
                    }
                />
            </Panel>
        );
    }

    // Resolve active KPI values for All Nodes vs Selected Node.

    const cpuMean = isAllNodes
        ? globalCpuMean
        : clampPercent(firstFinite(selectedRow?.cpu_usage_percent?.mean, selectedRow?.cpu_usage_percent));
    const cpuPeak = isAllNodes
        ? globalCpuPeak
        : clampPercent(firstFinite(selectedRow?.cpu_usage_percent?.max));

    const memTotalBytes = isAllNodes
        ? globalMemTotalBytes
        : firstFinite(selectedRow?.memory_total_bytes);
    const memAvailMeanBytes = isAllNodes
        ? globalMemAvailMeanBytes
        : firstFinite(selectedRow?.memory_available_bytes?.mean, selectedRow?.memory_available_bytes);
    const memAvailMinBytes = isAllNodes
        ? globalMemAvailMinBytes
        : firstFinite(selectedRow?.memory_available_bytes?.min, memAvailMeanBytes);
    const memUsedBytes = isAllNodes
        ? globalMemUsedBytes
        : firstFinite(
            selectedRow?.memory_used_bytes?.mean,
            selectedRow?.memory_used_bytes,
            (memTotalBytes !== null && memAvailMeanBytes !== null) ? Math.max(0, memTotalBytes - memAvailMeanBytes) : null,
        );
    const memPeakPct = isAllNodes
        ? globalMemPeakPct
        : clampPercent(firstFinite(
            selectedRow?.memory_usage_percent?.max,
            (memTotalBytes !== null && memTotalBytes > 0 && memAvailMinBytes !== null)
                ? ((memTotalBytes - memAvailMinBytes) / memTotalBytes) * 100
                : null,
        ));

    const diskReadBps = isAllNodes
        ? globalDiskReadBps
        : firstFinite(selectedRow?.disk_read_bytes_per_second?.mean, selectedRow?.disk_read_bytes_per_second);
    const diskWriteBps = isAllNodes
        ? globalDiskWriteBps
        : firstFinite(selectedRow?.disk_written_bytes_per_second?.mean, selectedRow?.disk_written_bytes_per_second);
    const diskFreeBytes = isAllNodes
        ? globalDiskFreeBytes
        : firstFinite(selectedRow?.filesystem_avail_bytes);
    const diskTotalBytes = isAllNodes
        ? globalDiskTotalBytes
        : firstFinite(selectedRow?.filesystem_size_bytes);

    const netRxBps = isAllNodes
        ? globalNetRxBps
        : firstFinite(selectedRow?.network_receive_bytes_per_second?.mean, selectedRow?.network_receive_bytes_per_second);
    const netTxBps = isAllNodes
        ? globalNetTxBps
        : firstFinite(selectedRow?.network_transmit_bytes_per_second?.mean, selectedRow?.network_transmit_bytes_per_second);
    const netRxPeakBps = isAllNodes
        ? globalNetRxPeakBps
        : firstFinite(selectedRow?.network_receive_bytes_per_second?.max);
    const netTxPeakBps = isAllNodes
        ? globalNetTxPeakBps
        : firstFinite(selectedRow?.network_transmit_bytes_per_second?.max);

    const hostPowerMean = isAllNodes
        ? globalHostPowerMean
        : firstFinite(selectedRow?.host_power_watts?.mean, selectedRow?.host_power_watts);
    const hostPowerPeak = isAllNodes
        ? globalHostPowerPeak
        : firstFinite(selectedRow?.host_power_watts?.max);
    const hostEnergyWh = isAllNodes
        ? globalHostEnergyWh
        : firstFinite(selectedRow?.host_energy_wh);

    const accelPowerMean = isAllNodes
        ? globalAccelPowerMean
        : firstFinite(selectedRow?.gpu_power_watts?.mean, selectedRow?.gpu_power_watts);
    const accelPowerPeak = isAllNodes
        ? globalAccelPowerPeak
        : firstFinite(selectedRow?.gpu_power_watts?.max);
    const accelTempPeak = isAllNodes
        ? globalAccelTempPeak
        : firstFinite(selectedRow?.gpu_temperature_celsius?.max, selectedRow?.gpu_temperature_celsius?.mean);

    // Chart availability flags for the active scope.
    const cpuChartData = overlayNodes.length >= 2 ? overlayMergedSeries : activeSeries;
    const hasCpuSeries = cpuChartData.some((pt) => (
        hasSeriesValue(pt, 'cpu_pct') || overlayNodes.some((n) => hasSeriesValue(pt, n.cpuKey))
    ));
    const hasMemUsedSeries = activeSeries.some((pt) => hasSeriesValue(pt, 'mem_used_bytes'));
    const hasMemAvailSeries = activeSeries.some((pt) => hasSeriesValue(pt, 'mem_avail_bytes'));
    const hasMemSeries = hasMemUsedSeries || hasMemAvailSeries;
    const memPctChartData = overlayNodes.length >= 2 ? overlayMergedSeries : activeSeries;
    const hasMemPctSeries = memPctChartData.some((pt) => (
        hasSeriesValue(pt, 'mem_pct') || overlayNodes.some((n) => hasSeriesValue(pt, n.memPctKey))
    ));
    const hasDiskSeries = activeSeries.some((pt) => hasSeriesValue(pt, 'disk_read_bps') || hasSeriesValue(pt, 'disk_write_bps'));
    const hasHostPowerSeries = activeSeries.some((pt) => hasSeriesValue(pt, 'host_power_w'));
    const hasAccelPowerSeries = activeSeries.some((pt) => hasSeriesValue(pt, 'accel_power_w'));

    const ioOptions = [
        ...(hasMemSeries ? [{ value: 'memory', label: 'Memory' }] : []),
        ...(hasMemPctSeries ? [{ value: 'memory_pct', label: 'Memory %' }] : []),
        ...(hasDiskSeries ? [{ value: 'disk', label: 'Disk I/O' }] : []),
    ];
    const activeIoView = (ioView && ioOptions.some((opt) => opt.value === ioView))
        ? ioView
        : (ioOptions[0]?.value ?? 'memory');

    const toggleNodeScope = (node) => {
        setSelectedNode((prev) => (prev === node ? ALL_NODES_SCOPE : node));
    };

    return (
        <div className="space-y-6">
            {/* Target Nodes & Node Scope Selector */}
            {nodeList.length > 0 && (
                <Panel padding="sm" className="flex flex-wrap items-center justify-between gap-3">
                    <div className="flex flex-wrap items-center gap-2">
                        <span className="inline-flex items-center gap-1 text-xs font-semibold text-theme-muted">
                            <span>Target Nodes:</span>
                            <HelpTooltip label="Target nodes help" align="start" iconSize={12}>
                                Kubernetes worker nodes hosting the serving pods monitored via node_exporter and host power exporters during this run. Click a node to focus KPI cards and charts.
                            </HelpTooltip>
                        </span>
                        {nodeList.map((node) => {
                            const isSelected = !isAllNodes && activeNode === node;
                            return (
                                <button
                                    key={node}
                                    type="button"
                                    onClick={() => toggleNodeScope(node)}
                                    aria-pressed={isSelected}
                                    title={isSelected ? `Show all nodes (clear ${node} filter)` : `Focus KPI cards and charts on ${node}`}
                                    className="rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50 cursor-pointer"
                                >
                                    <Badge
                                        tone={isSelected ? 'brand' : 'neutral'}
                                        size="sm"
                                    >
                                        {node}
                                    </Badge>
                                </button>
                            );
                        })}
                    </div>

                    <div className="flex flex-wrap items-center gap-2">
                        <span className="inline-flex items-center gap-1 text-xs font-semibold text-theme-muted">
                            <span>Scope:</span>
                            <HelpTooltip label="Node scope help" align="end" iconSize={12}>
                                Switch KPI cards and time-series charts between the global cluster aggregate and an individual serving node.
                            </HelpTooltip>
                        </span>
                        {nodeList.length <= 5 ? (
                            <ToggleGroup
                                size="xs"
                                value={activeNode}
                                onChange={setSelectedNode}
                                options={nodeScopeOptions}
                            />
                        ) : (
                            <Select
                                aria-label="Node scope"
                                value={activeNode}
                                onChange={(event) => setSelectedNode(event.target.value)}
                                className="h-8 w-56 py-1 text-xs"
                            >
                                {nodeScopeOptions.map((opt) => (
                                    <option key={opt.value} value={opt.value}>{opt.label}</option>
                                ))}
                            </Select>
                        )}
                    </div>
                </Panel>
            )}

            {/* 6 Host & Accelerator KPI Cards */}
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                <StatCard
                    compact
                    icon={<Cpu className="h-4 w-4" />}
                    title="Host CPU Utilization"
                    help={`Mean host CPU utilization (%) for ${scopeLabel} from node_exporter. Sustained high CPU (>80%) can bottleneck tokenization, HTTP serving, or KV transfers.`}
                    helpFormula="CPU_Mean (%) = mean_t ((1 − ΔIdle_CPU_Time / ΔTotal_CPU_Time) × 100)"
                    helpAlign="start"
                    value={cpuMean !== null ? formatNumber(cpuMean, '%') : 'Unavailable'}
                    details={[
                        {
                            label: 'Peak CPU',
                            value: formatNumber(cpuPeak, '%'),
                            help: 'Maximum host CPU utilization (%) observed across any scrape interval.',
                            formula: 'Peak_CPU (%) = max_t ((1 − ΔIdle_CPU_Time / ΔTotal_CPU_Time) × 100)',
                        },
                    ]}
                />
                <StatCard
                    compact
                    icon={<Server className="h-4 w-4" />}
                    title="Host Memory Usage"
                    help={`Mean host system RAM used out of total capacity for ${scopeLabel} (automatically scaled across B to PiB). Tight memory headroom increases the risk of OOM kills or CPU KV-offload thrashing.`}
                    helpFormula="Mem_Used = Total_RAM − Available_RAM  (scaled across B, KiB, MiB, GiB, TiB, PiB)"
                    helpAlign="center"
                    value={
                        (memUsedBytes !== null && memTotalBytes !== null)
                            ? formatBytePair(memUsedBytes, memTotalBytes)
                            : memUsedBytes !== null
                                ? formatBytes(memUsedBytes)
                                : memAvailMeanBytes !== null
                                    ? `${formatBytes(memAvailMeanBytes)} avail`
                                    : 'Unavailable'
                    }
                    details={[
                        {
                            label: 'Peak Usage',
                            value: memPeakPct !== null ? formatNumber(memPeakPct, '%') : '—',
                            help: 'Maximum host memory utilization (%) observed across scrape intervals.',
                            formula: 'Peak_Mem (%) = max_t (((Total_RAM − Available_RAM(t)) / Total_RAM) × 100)',
                        },
                        {
                            label: 'Min Available',
                            value: memAvailMinBytes !== null ? formatBytes(memAvailMinBytes) : '—',
                            help: 'Minimum free/available host memory observed across scrape intervals (scaled across B to PiB).',
                            formula: 'Min_Avail = min_t (Available_RAM(t))',
                        },
                    ]}
                />
                <StatCard
                    compact
                    icon={<HardDrive className="h-4 w-4" />}
                    title="Disk I/O & Capacity"
                    help={`Mean host block-device read and write throughput and local filesystem capacity for ${scopeLabel} from node_exporter (automatically scaled across B to PiB and B/s to PiB/s).`}
                    helpFormula="Disk_Rate = mean_t (ΔDisk_Bytes / Δt)  (scaled across B/s, KiB/s, MiB/s, GiB/s, TiB/s, PiB/s)"
                    helpAlign="end"
                    value={
                        formatByteRatePair(diskReadBps, diskWriteBps)
                            ?? (diskFreeBytes !== null
                                ? `${formatBytes(diskFreeBytes)} avail`
                                : 'Unavailable')
                    }
                    details={[
                        {
                            label: 'Free / Total Disk',
                            value: (diskFreeBytes !== null && diskTotalBytes !== null)
                                ? formatBytePair(diskFreeBytes, diskTotalBytes)
                                : diskFreeBytes !== null
                                    ? formatBytes(diskFreeBytes)
                                    : '—',
                            help: 'Minimum available and total local filesystem capacity on target nodes (scaled across B to PiB).',
                            formula: 'Free_Disk = Available_FS_Bytes;  Total_Disk = Total_FS_Bytes',
                        },
                    ]}
                />
                <StatCard
                    compact
                    icon={<Network className="h-4 w-4" />}
                    title="Network Throughput"
                    help={`Mean host network receive (RX) and transmit (TX) throughput for ${scopeLabel} from node_exporter (automatically scaled across B/s to PiB/s).`}
                    helpFormula="Net_Rate = mean_t (ΔNetwork_Bytes / Δt)  (scaled across B/s, KiB/s, MiB/s, GiB/s, TiB/s, PiB/s)"
                    helpAlign="start"
                    value={formatByteRatePair(netRxBps, netTxBps) ?? 'Unavailable'}
                    details={[
                        {
                            label: 'Peak RX / TX',
                            value: formatByteRatePair(netRxPeakBps, netTxPeakBps) ?? '—',
                            help: 'Maximum host network receive (RX) and transmit (TX) throughput observed during the run (scaled across B/s to PiB/s).',
                            formula: 'Peak_Net = max_t (ΔNetwork_Bytes / Δt)',
                        },
                    ]}
                />
                <StatCard
                    compact
                    icon={<Zap className="h-4 w-4" />}
                    title="Host Power"
                    help={`Mean full-node host power draw (W) for ${scopeLabel} from IPMI, RAPL, or Kepler exporters.`}
                    helpFormula="Host_Power_Mean (W) = mean_t Host_Power_W(t)"
                    helpAlign="center"
                    value={hostPowerMean !== null ? formatWatts(hostPowerMean) : 'Unavailable'}
                    details={[
                        {
                            label: 'Peak Power',
                            value: hostPowerPeak !== null ? formatWatts(hostPowerPeak) : '—',
                            help: 'Maximum instantaneous host node power draw (W) observed during the run.',
                            formula: 'Host_Peak_Power (W) = max_t Host_Power_W(t)',
                        },
                        ...(hostEnergyWh !== null ? [{
                            label: 'Host Energy',
                            value: formatEnergyWh(hostEnergyWh),
                            help: 'Cumulative host node energy consumed (Wh) over the telemetry window.',
                            formula: 'Host_Energy_Wh = (1 / 3600) Σ 0.5 × (P_i + P_{i+1}) × Δt_i  (skipping Δt > 3×step)',
                        }] : []),
                    ]}
                />
                <StatCard
                    compact
                    icon={<Thermometer className="h-4 w-4" />}
                    title={`${accelLabel} Power & Thermal`}
                    help={`Mean power draw (W) and peak thermal reading (°C) across serving ${accelLabel.toLowerCase()} devices for ${scopeLabel}.`}
                    helpFormula="Accel_Power_Mean (W) = mean_t (Σ_devices Accel_Power_W(t))"
                    helpAlign="end"
                    value={accelPowerMean !== null ? formatWatts(accelPowerMean) : 'Unavailable'}
                    details={[
                        {
                            label: 'Peak Power',
                            value: accelPowerPeak !== null ? formatWatts(accelPowerPeak) : '—',
                            help: `Maximum ${accelLabel.toLowerCase()} power draw (W) observed during the run.`,
                            formula: 'Accel_Peak_Power (W) = max_t (Σ_devices Accel_Power_W(t))',
                        },
                        {
                            label: 'Peak Temp',
                            value: accelTempPeak !== null ? `${formatNumber(accelTempPeak)} °C` : '—',
                            help: `Maximum ${accelLabel.toLowerCase()} temperature (°C) reported during the run.`,
                            formula: 'Accel_Peak_Temp (°C) = max_{t,device} Temp_C(t)',
                        },
                    ]}
                />
            </div>

            {!hasAnyHostMetric && (
                <Panel padding="sm">
                    <p className="text-xs text-theme-muted">
                        Metric unavailable (node_exporter not detected for the target cluster; showing available {accelLabel.toLowerCase()} power telemetry below).
                    </p>
                </Panel>
            )}

            {/* Rich Per-Node Summary Table */}
            {perNodeRows.length > 0 && (
                <Panel
                    title="Per-Node Summary"
                    help="Per-node host resource, network, power, and accelerator breakdown for every serving node. Click a row to focus the KPI cards and charts on that node."
                    helpFormula="Per-node mean and peak aggregates over telemetry scrape intervals [t_start, t_end]"
                    helpAlign="start"
                    actions={!isAllNodes ? (
                        <Button
                            size="xs"
                            variant="ghost"
                            onClick={() => setSelectedNode(ALL_NODES_SCOPE)}
                        >
                            Reset to All Nodes
                        </Button>
                    ) : (
                        <span className="text-[11px] text-theme-muted">Click a row to drill down</span>
                    )}
                >
                    <div className="overflow-x-auto lg:overflow-visible rounded-lg border border-theme-border">
                        <table className="w-full text-left text-xs">
                            <thead className="border-b border-theme-border text-[10px] uppercase tracking-wider text-theme-muted">
                                <tr>
                                    <th className="px-3 py-2">
                                        <span className="inline-flex items-center gap-1">
                                            <span>Node</span>
                                            <HelpTooltip label="Node column help" align="start" iconSize={11}>
                                                Kubernetes worker node hostname running serving pods during the simulation.
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2">
                                        <span className="inline-flex items-center gap-1">
                                            <span>Roles &amp; Pods</span>
                                            <HelpTooltip label="Roles and Pods column help" align="start" iconSize={11}>
                                                Serving roles (e.g., Prefill, Decode, Worker) and pods scheduled on this node.
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2 text-right">
                                        <span className="inline-flex items-center justify-end gap-1">
                                            <span>CPU (Mean / Peak)</span>
                                            <HelpTooltip
                                                label="CPU Mean and Peak column help"
                                                align="center"
                                                iconSize={11}
                                                formula="CPU (%) = (1 − ΔIdle_CPU_Time / ΔTotal_CPU_Time) × 100  (mean / max)"
                                            >
                                                Mean and maximum host CPU utilization (%) on this node.
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2 text-right">
                                        <span className="inline-flex items-center justify-end gap-1">
                                            <span>Memory (Used / Total · Peak %)</span>
                                            <HelpTooltip
                                                label="Memory Used and Total column help"
                                                align="center"
                                                iconSize={11}
                                                formula="Used = Total − Avail;  Peak_% = ((Total − Min_Avail) / Total) × 100"
                                            >
                                                Mean host RAM used out of total capacity (adaptively scaled across B to PiB) and peak memory utilization (%).
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2 text-right">
                                        <span className="inline-flex items-center justify-end gap-1">
                                            <span>Disk I/O (R / W)</span>
                                            <HelpTooltip
                                                label="Disk I/O column help"
                                                align="center"
                                                iconSize={11}
                                                formula="Disk_IO = mean_t (ΔRead_or_Write_Bytes / Δt)  (scaled across B/s to PiB/s)"
                                            >
                                                Mean host block-device read and write throughput on this node (adaptively scaled across B/s to PiB/s).
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2 text-right">
                                        <span className="inline-flex items-center justify-end gap-1">
                                            <span>Free Disk</span>
                                            <HelpTooltip
                                                label="Free Disk column help"
                                                align="end"
                                                iconSize={11}
                                                formula="Free_Disk = Available_FS_Bytes  (scaled across B to PiB)"
                                            >
                                                Available local filesystem space reported by node_exporter (adaptively scaled across B to PiB).
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2 text-right">
                                        <span className="inline-flex items-center justify-end gap-1">
                                            <span>Network (RX / TX)</span>
                                            <HelpTooltip
                                                label="Network RX and TX column help"
                                                align="end"
                                                iconSize={11}
                                                formula="Net_IO = mean_t (ΔRX_or_TX_Bytes / Δt)  (scaled across B/s to PiB/s)"
                                            >
                                                Mean host network receive (RX) and transmit (TX) throughput (adaptively scaled across B/s to PiB/s).
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2 text-right">
                                        <span className="inline-flex items-center justify-end gap-1">
                                            <span>Host Power (Mean / Peak)</span>
                                            <HelpTooltip
                                                label="Host Power column help"
                                                align="end"
                                                iconSize={11}
                                                formula="Mean = mean_t Host_Power_W(t);  Peak = max_t Host_Power_W(t)"
                                            >
                                                Mean and peak full-node host power draw (W) measured on this node.
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                    <th className="px-3 py-2 text-right">
                                        <span className="inline-flex items-center justify-end gap-1">
                                            <span>{accelLabel} (Count · Util · Power · Temp)</span>
                                            <HelpTooltip
                                                label={`${accelLabel} summary column help`}
                                                align="end"
                                                iconSize={11}
                                                formula="Count = Allocated_Devices;  Util = mean_t Util_%;  Power = mean_t W;  Temp = max_t °C"
                                            >
                                                Allocated {accelLabel.toLowerCase()} count, mean utilization (%), mean power (W), and peak temperature (°C) on this node.
                                            </HelpTooltip>
                                        </span>
                                    </th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-theme-border">
                                {perNodeRows.map((row) => {
                                    const isSelected = activeNode === row.node;
                                    const roles = Array.isArray(row.roles) ? row.roles : EMPTY_ARR;
                                    const pods = Array.isArray(row.pods) ? row.pods : EMPTY_ARR;
                                    const podCount = firstFinite(row.pod_count, pods.length);

                                    const rowCpuMean = clampPercent(firstFinite(row.cpu_usage_percent?.mean, row.cpu_usage_percent));
                                    const rowCpuPeak = clampPercent(firstFinite(row.cpu_usage_percent?.max));
                                    const rowMemTotalBytes = firstFinite(row.memory_total_bytes);
                                    const rowMemAvailBytes = firstFinite(row.memory_available_bytes?.mean, row.memory_available_bytes);
                                    const rowMemAvailMinBytes = firstFinite(row.memory_available_bytes?.min, rowMemAvailBytes);
                                    const rowMemUsedBytes = firstFinite(
                                        row.memory_used_bytes?.mean,
                                        row.memory_used_bytes,
                                        (rowMemTotalBytes !== null && rowMemAvailBytes !== null)
                                            ? Math.max(0, rowMemTotalBytes - rowMemAvailBytes)
                                            : null,
                                    );
                                    const rowMemPeakPct = clampPercent(firstFinite(
                                        row.memory_usage_percent?.max,
                                        (rowMemTotalBytes !== null && rowMemTotalBytes > 0 && rowMemAvailMinBytes !== null)
                                            ? ((rowMemTotalBytes - rowMemAvailMinBytes) / rowMemTotalBytes) * 100
                                            : null,
                                    ));
                                    const rowDiskReadBps = firstFinite(row.disk_read_bytes_per_second?.mean, row.disk_read_bytes_per_second);
                                    const rowDiskWriteBps = firstFinite(row.disk_written_bytes_per_second?.mean, row.disk_written_bytes_per_second);
                                    const rowFreeDiskBytes = firstFinite(row.filesystem_avail_bytes);
                                    const rowNetRxBps = firstFinite(row.network_receive_bytes_per_second?.mean, row.network_receive_bytes_per_second);
                                    const rowNetTxBps = firstFinite(row.network_transmit_bytes_per_second?.mean, row.network_transmit_bytes_per_second);
                                    const rowHostPowerMean = firstFinite(row.host_power_watts?.mean, row.host_power_watts);
                                    const rowHostPowerPeak = firstFinite(row.host_power_watts?.max);

                                    const accelCount = firstFinite(row.allocated_accelerators);
                                    const accelUtil = clampPercent(firstFinite(row.gpu_utilization_percent?.mean, row.gpu_utilization_percent));
                                    const accelPower = firstFinite(row.gpu_power_watts?.mean, row.gpu_power_watts);
                                    const accelTemp = firstFinite(row.gpu_temperature_celsius?.max, row.gpu_temperature_celsius?.mean);

                                    return (
                                        <tr
                                            key={row.node}
                                            role="button"
                                            tabIndex={0}
                                            aria-pressed={isSelected}
                                            onClick={() => toggleNodeScope(row.node)}
                                            onKeyDown={(event) => {
                                                if (event.key === 'Enter' || event.key === ' ') {
                                                    event.preventDefault();
                                                    toggleNodeScope(row.node);
                                                }
                                            }}
                                            className={cn(
                                                'text-theme-text cursor-pointer transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-emerald-500/50',
                                                isSelected
                                                    ? 'bg-emerald-500/10 dark:bg-emerald-500/15'
                                                    : 'hover:bg-slate-50/80 dark:hover:bg-slate-800/40',
                                            )}
                                        >
                                            <td className="px-3 py-2 font-mono font-semibold whitespace-nowrap">
                                                <span className="inline-flex items-center gap-1.5">
                                                    <span>{row.node}</span>
                                                    {isSelected && <Badge tone="brand" size="xs">Focused</Badge>}
                                                </span>
                                            </td>
                                            <td className="px-3 py-2">
                                                <div className="flex flex-wrap items-center gap-1">
                                                    {roles.map((role) => (
                                                        <Badge key={role} tone={roleBadgeTone(role)} size="xs">
                                                            {displayName(role)}
                                                        </Badge>
                                                    ))}
                                                    {podCount !== null && (
                                                        <span className="text-[11px] text-theme-muted" title={pods.join(', ')}>
                                                            {formatCount(podCount)} {podCount === 1 ? 'pod' : 'pods'}
                                                        </span>
                                                    )}
                                                </div>
                                                {pods.length > 0 && (
                                                    <div className="mt-0.5 max-w-[14rem] truncate font-mono text-[10px] text-theme-muted" title={pods.join(', ')}>
                                                        {pods.join(', ')}
                                                    </div>
                                                )}
                                            </td>
                                            <td className="px-3 py-2 text-right font-mono whitespace-nowrap">
                                                {rowCpuMean !== null ? formatNumber(rowCpuMean, '%') : '—'}
                                                {' / '}
                                                {rowCpuPeak !== null ? formatNumber(rowCpuPeak, '%') : '—'}
                                            </td>
                                            <td className="px-3 py-2 text-right font-mono whitespace-nowrap">
                                                {(rowMemUsedBytes !== null && rowMemTotalBytes !== null)
                                                    ? formatBytePair(rowMemUsedBytes, rowMemTotalBytes)
                                                    : `${rowMemUsedBytes !== null ? formatBytes(rowMemUsedBytes) : '—'} / ${rowMemTotalBytes !== null ? formatBytes(rowMemTotalBytes) : '—'}`}
                                                {' · '}
                                                {rowMemPeakPct !== null ? formatNumber(rowMemPeakPct, '%') : '—'}
                                            </td>
                                            <td className="px-3 py-2 text-right font-mono whitespace-nowrap">
                                                {formatByteRatePair(rowDiskReadBps, rowDiskWriteBps) ?? '—'}
                                            </td>
                                            <td className="px-3 py-2 text-right font-mono whitespace-nowrap">
                                                {rowFreeDiskBytes !== null ? formatBytes(rowFreeDiskBytes) : '—'}
                                            </td>
                                            <td className="px-3 py-2 text-right font-mono whitespace-nowrap">
                                                {formatByteRatePair(rowNetRxBps, rowNetTxBps) ?? '—'}
                                            </td>
                                            <td className="px-3 py-2 text-right font-mono whitespace-nowrap">
                                                {rowHostPowerMean !== null ? formatWatts(rowHostPowerMean) : '—'}
                                                {' / '}
                                                {rowHostPowerPeak !== null ? formatWatts(rowHostPowerPeak) : '—'}
                                            </td>
                                            <td className="px-3 py-2 text-right font-mono whitespace-nowrap">
                                                {accelCount !== null ? `${formatCount(accelCount)}x` : '—'}
                                                {' · '}
                                                {accelUtil !== null ? formatNumber(accelUtil, '%') : '—'}
                                                {' · '}
                                                {accelPower !== null ? formatWatts(accelPower) : '—'}
                                                {' · '}
                                                {accelTemp !== null ? `${formatNumber(accelTemp)} °C` : '—'}
                                            </td>
                                        </tr>
                                    );
                                })}
                            </tbody>
                        </table>
                    </div>
                </Panel>
            )}

            {/* Synchronized Charts Node Scope Bar */}
            {nodeList.length > 0 && (
                <Panel padding="sm" className="flex flex-wrap items-center justify-between gap-3">
                    <div className="flex flex-wrap items-center gap-2">
                        <span className="inline-flex items-center gap-1 text-xs font-semibold text-theme-muted">
                            <span>Charts Scope:</span>
                            <HelpTooltip
                                label="Charts node scope help"
                                align="start"
                                iconSize={12}
                                formula="Scope ∈ {All Nodes (Aggregate)} ∪ Target_Nodes"
                            >
                                Switch all four host telemetry charts between the cluster-wide aggregate and a single serving node. Synchronized across the target node controls and charts.
                            </HelpTooltip>
                        </span>
                    </div>
                    {nodeList.length <= 5 ? (
                        <ToggleGroup
                            size="xs"
                            value={activeNode}
                            onChange={setSelectedNode}
                            options={nodeScopeOptions}
                        />
                    ) : (
                        <Select
                            aria-label="Charts node scope"
                            value={activeNode}
                            onChange={(event) => setSelectedNode(event.target.value)}
                            className="h-8 w-56 py-1 text-xs"
                        >
                            {nodeScopeOptions.map((opt) => (
                                <option key={opt.value} value={opt.value}>{opt.label}</option>
                            ))}
                        </Select>
                    )}
                </Panel>
            )}

            {/* 2x2 Grid of 4 Crisp Time-Series Charts */}
            <div className="grid gap-6 xl:grid-cols-2">
                {/* Chart 1: Host CPU Utilization over Time */}
                <ChartContainer
                    title="Host CPU Utilization over Time"
                    subtitle={
                        isAllNodes
                            ? (overlayNodes.length >= 2
                                ? 'Aggregate & per-node host CPU % over elapsed time'
                                : 'Node CPU % across target model-serving nodes')
                            : `Host CPU % on ${activeNode}`
                    }
                    help="Host CPU utilization (%) over elapsed run time from node_exporter. Sustained high CPU signals tokenization, HTTP request handling, or CPU KV-offload pressure."
                    helpFormula="CPU(t) (%) = (1 − ΔIdle_CPU_Time / ΔTotal_CPU_Time) × 100"
                    helpAlign="start"
                >
                    {hasCpuSeries ? (
                        <>
                            <div className="h-56 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <LineChart data={cpuChartData} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                        <YAxis domain={[0, 100]} ticks={[0, 25, 50, 75, 100]} allowDataOverflow width={48} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: '%', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                        <Tooltip cursor={LINE_TOOLTIP_CURSOR} content={<UnitTooltip unit="%" />} />
                                        <Line
                                            type="monotone"
                                            dataKey="cpu_pct"
                                            name={isAllNodes ? 'Aggregate CPU %' : `${activeNode} CPU %`}
                                            stroke={CHART_SERIES[0]}
                                            strokeWidth={2}
                                            connectNulls
                                            dot={sparseDotProp(cpuChartData, 'cpu_pct', CHART_SERIES[0])}
                                            isAnimationActive={false}
                                        />
                                        {overlayNodes.map((nodeInfo) => (
                                            <Line
                                                key={nodeInfo.node}
                                                type="monotone"
                                                dataKey={nodeInfo.cpuKey}
                                                name={nodeInfo.node}
                                                stroke={nodeInfo.color}
                                                strokeWidth={1.25}
                                                strokeDasharray="4 2"
                                                connectNulls
                                                dot={false}
                                                isAnimationActive={false}
                                            />
                                        ))}
                                    </LineChart>
                                </ResponsiveContainer>
                            </div>
                            {overlayNodes.length >= 2 && (
                                <ChartLegend
                                    className="mt-3"
                                    entries={[
                                        { label: 'Aggregate CPU %', color: CHART_SERIES[0] },
                                        ...overlayNodes.map((n) => ({ label: n.node, color: n.color })),
                                    ]}
                                />
                            )}
                        </>
                    ) : (
                        <EmptyState
                            icon={<Cpu className="h-6 w-6" />}
                            title="Host CPU metric unavailable"
                            message="Metric unavailable (node_exporter CPU series not detected)."
                        />
                    )}
                </ChartContainer>

                {/* Chart 2: Host Memory & Disk I/O */}
                <ChartContainer
                    title="Host Memory & Disk I/O"
                    subtitle={
                        activeIoView === 'disk'
                            ? (hasDiskSeries
                                ? `Block-device read and write throughput (${diskScale.unit}) · ${scopeLabel}`
                                : `Block-device read and write throughput · ${scopeLabel}`)
                            : activeIoView === 'memory_pct'
                                ? (overlayNodes.length >= 2
                                    ? 'Aggregate & per-node memory utilization (%)'
                                    : `Host memory utilization (%) · ${scopeLabel}`)
                                : (hasMemSeries
                                    ? `Host used vs available memory (${memScale.unit}) · ${scopeLabel}`
                                    : `Host used vs available memory · ${scopeLabel}`)
                    }
                    help="Time series of host memory headroom (adaptively scaled across B to PiB, or %) and block-device read/write throughput (adaptively scaled across B/s to PiB/s) from node_exporter."
                    helpFormula="Mem_Used = Total − Avail;  Mem_% = (Used / Total) × 100;  Disk_IO = ΔBytes / Δt  (scaled across B..PiB / B/s..PiB/s)"
                    helpAlign="end"
                    actions={ioOptions.length > 1 ? (
                        <ToggleGroup
                            size="xs"
                            value={activeIoView}
                            onChange={setIoView}
                            options={ioOptions}
                        />
                    ) : null}
                >
                    {activeIoView === 'disk' && hasDiskSeries ? (
                        <>
                            <div className="h-56 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <LineChart data={diskChartData} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                        <YAxis width={56} tickFormatter={formatAxisTick} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: diskScale.unit, angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                        <Tooltip cursor={LINE_TOOLTIP_CURSOR} content={<UnitTooltip unit={diskScale.unit} />} />
                                        <Line type="monotone" dataKey="disk_read" name="Disk Read" stroke={CHART_SERIES[1]} strokeWidth={2} connectNulls dot={sparseDotProp(diskChartData, 'disk_read', CHART_SERIES[1])} isAnimationActive={false} />
                                        <Line type="monotone" dataKey="disk_write" name="Disk Write" stroke={CHART_SERIES[2]} strokeWidth={2} connectNulls dot={sparseDotProp(diskChartData, 'disk_write', CHART_SERIES[2])} isAnimationActive={false} />
                                    </LineChart>
                                </ResponsiveContainer>
                            </div>
                            <ChartLegend
                                className="mt-3"
                                entries={[
                                    { label: `Disk Read (${diskScale.unit})`, color: CHART_SERIES[1] },
                                    { label: `Disk Write (${diskScale.unit})`, color: CHART_SERIES[2] },
                                ]}
                            />
                        </>
                    ) : activeIoView === 'memory_pct' && hasMemPctSeries ? (
                        <>
                            <div className="h-56 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <LineChart data={memPctChartData} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                        <YAxis domain={[0, 100]} ticks={[0, 25, 50, 75, 100]} allowDataOverflow width={48} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: '%', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                        <Tooltip cursor={LINE_TOOLTIP_CURSOR} content={<UnitTooltip unit="%" />} />
                                        <Line
                                            type="monotone"
                                            dataKey="mem_pct"
                                            name={isAllNodes ? 'Aggregate Memory %' : `${activeNode} Memory %`}
                                            stroke={CHART_SERIES[0]}
                                            strokeWidth={2}
                                            connectNulls
                                            dot={sparseDotProp(memPctChartData, 'mem_pct', CHART_SERIES[0])}
                                            isAnimationActive={false}
                                        />
                                        {overlayNodes.map((nodeInfo) => (
                                            <Line
                                                key={nodeInfo.node}
                                                type="monotone"
                                                dataKey={nodeInfo.memPctKey}
                                                name={nodeInfo.node}
                                                stroke={nodeInfo.color}
                                                strokeWidth={1.25}
                                                strokeDasharray="4 2"
                                                connectNulls
                                                dot={false}
                                                isAnimationActive={false}
                                            />
                                        ))}
                                    </LineChart>
                                </ResponsiveContainer>
                            </div>
                            {overlayNodes.length >= 2 && (
                                <ChartLegend
                                    className="mt-3"
                                    entries={[
                                        { label: 'Aggregate Memory %', color: CHART_SERIES[0] },
                                        ...overlayNodes.map((n) => ({ label: n.node, color: n.color })),
                                    ]}
                                />
                            )}
                        </>
                    ) : hasMemSeries ? (
                        <>
                            <div className="h-56 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <LineChart data={memChartData} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                        <YAxis width={56} tickFormatter={formatAxisTick} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: memScale.unit, angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                        <Tooltip cursor={LINE_TOOLTIP_CURSOR} content={<UnitTooltip unit={memScale.unit} />} />
                                        {hasMemUsedSeries && (
                                            <Line type="monotone" dataKey="mem_used" name="Used Memory" stroke={CHART_SERIES[0]} strokeWidth={2} connectNulls dot={sparseDotProp(memChartData, 'mem_used', CHART_SERIES[0])} isAnimationActive={false} />
                                        )}
                                        {hasMemAvailSeries && (
                                            <Line type="monotone" dataKey="mem_avail" name="Available Memory" stroke={CHART_SERIES[1]} strokeWidth={2} connectNulls dot={sparseDotProp(memChartData, 'mem_avail', CHART_SERIES[1])} isAnimationActive={false} />
                                        )}
                                    </LineChart>
                                </ResponsiveContainer>
                            </div>
                            {(hasMemUsedSeries && hasMemAvailSeries) && (
                                <ChartLegend
                                    className="mt-3"
                                    entries={[
                                        { label: `Used Memory (${memScale.unit})`, color: CHART_SERIES[0] },
                                        { label: `Available Memory (${memScale.unit})`, color: CHART_SERIES[1] },
                                    ]}
                                />
                            )}
                        </>
                    ) : (
                        <EmptyState
                            icon={<HardDrive className="h-6 w-6" />}
                            title="Memory & disk I/O unavailable"
                            message="Metric unavailable (node_exporter memory and disk I/O series not detected)."
                        />
                    )}
                </ChartContainer>

                {/* Chart 3: Network RX / TX Throughput */}
                <ChartContainer
                    title="Network RX / TX Throughput"
                    subtitle={netPlot.subtitle}
                    help="Host network receive (RX) and transmit (TX) throughput over elapsed run time from node_exporter (adaptively scaled across B/s to PiB/s), with optional per-interface breakdown excluding virtual container interfaces. Spikes during disaggregated serving reflect KV-cache or tensor transfers."
                    helpFormula="Net_RX_or_TX = ΔNetwork_Bytes / Δt  (scaled across B/s..PiB/s);  By_NIC = Σ_{i∈[0..3]} NIC_i + Other"
                    helpAlign="start"
                    actions={nicOptions.length > 0 ? (
                        nicOptions.length <= 4 ? (
                            <ToggleGroup
                                size="xs"
                                value={activeNic}
                                onChange={setSelectedNic}
                                options={nicOptions}
                            />
                        ) : (
                            <Select
                                aria-label="Network interface"
                                value={activeNic}
                                onChange={(event) => setSelectedNic(event.target.value)}
                                className="h-7 w-44 py-0.5 text-xs"
                            >
                                {nicOptions.map((opt) => (
                                    <option key={opt.value} value={opt.value}>{opt.label}</option>
                                ))}
                            </Select>
                        )
                    ) : null}
                >
                    {netPlot.hasData ? (
                        <>
                            <div className="h-56 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <LineChart data={netPlot.data} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                        <YAxis width={56} tickFormatter={formatAxisTick} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: netPlot.unit, angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                        <Tooltip cursor={LINE_TOOLTIP_CURSOR} content={<UnitTooltip unit={netPlot.unit} />} />
                                        {netPlot.mode === 'by_nic' ? (
                                            netPlot.slots.flatMap((slot) => {
                                                const lines = [];
                                                if (slot.hasRx) {
                                                    lines.push(
                                                        <Line
                                                            key={slot.rxKey}
                                                            type="monotone"
                                                            dataKey={slot.rxKey}
                                                            name={`${slot.label} RX`}
                                                            stroke={slot.color}
                                                            strokeWidth={2}
                                                            connectNulls
                                                            dot={sparseDotProp(netPlot.data, slot.rxKey, slot.color)}
                                                            isAnimationActive={false}
                                                        />,
                                                    );
                                                }
                                                if (slot.hasTx) {
                                                    lines.push(
                                                        <Line
                                                            key={slot.txKey}
                                                            type="monotone"
                                                            dataKey={slot.txKey}
                                                            name={`${slot.label} TX`}
                                                            stroke={slot.color}
                                                            strokeWidth={1.75}
                                                            strokeDasharray="4 2"
                                                            connectNulls
                                                            dot={sparseDotProp(netPlot.data, slot.txKey, slot.color)}
                                                            isAnimationActive={false}
                                                        />,
                                                    );
                                                }
                                                return lines;
                                            })
                                        ) : netPlot.mode === 'single_nic' ? (
                                            <>
                                                {netPlot.hasRx && (
                                                    <Line type="monotone" dataKey="sel_nic_rx" name={`${activeNic} RX`} stroke={CHART_SERIES[0]} strokeWidth={2} connectNulls dot={sparseDotProp(netPlot.data, 'sel_nic_rx', CHART_SERIES[0])} isAnimationActive={false} />
                                                )}
                                                {netPlot.hasTx && (
                                                    <Line type="monotone" dataKey="sel_nic_tx" name={`${activeNic} TX`} stroke={CHART_SERIES[1]} strokeWidth={2} connectNulls dot={sparseDotProp(netPlot.data, 'sel_nic_tx', CHART_SERIES[1])} isAnimationActive={false} />
                                                )}
                                            </>
                                        ) : (
                                            <>
                                                {netPlot.hasRx && (
                                                    <Line type="monotone" dataKey="net_rx" name="Network RX" stroke={CHART_SERIES[0]} strokeWidth={2} connectNulls dot={sparseDotProp(netPlot.data, 'net_rx', CHART_SERIES[0])} isAnimationActive={false} />
                                                )}
                                                {netPlot.hasTx && (
                                                    <Line type="monotone" dataKey="net_tx" name="Network TX" stroke={CHART_SERIES[1]} strokeWidth={2} connectNulls dot={sparseDotProp(netPlot.data, 'net_tx', CHART_SERIES[1])} isAnimationActive={false} />
                                                )}
                                            </>
                                        )}
                                    </LineChart>
                                </ResponsiveContainer>
                            </div>
                            {netPlot.mode === 'by_nic' ? (
                                (netPlot.slots.reduce((acc, s) => acc + (s.hasRx ? 1 : 0) + (s.hasTx ? 1 : 0), 0) >= 2) && (
                                    <ChartLegend
                                        className="mt-3"
                                        entries={netPlot.slots.map((slot) => ({
                                            label: (slot.hasRx && slot.hasTx)
                                                ? `${slot.label} (RX solid · TX dashed)`
                                                : slot.hasRx
                                                    ? `${slot.label} RX`
                                                    : `${slot.label} TX`,
                                            color: slot.color,
                                        }))}
                                    />
                                )
                            ) : netPlot.mode === 'single_nic' ? (
                                (netPlot.hasRx && netPlot.hasTx) && (
                                    <ChartLegend
                                        className="mt-3"
                                        entries={[
                                            { label: `${activeNic} RX (${netPlot.unit})`, color: CHART_SERIES[0] },
                                            { label: `${activeNic} TX (${netPlot.unit})`, color: CHART_SERIES[1] },
                                        ]}
                                    />
                                )
                            ) : (
                                (netPlot.hasRx && netPlot.hasTx) && (
                                    <ChartLegend
                                        className="mt-3"
                                        entries={[
                                            { label: `Network RX (${netPlot.unit})`, color: CHART_SERIES[0] },
                                            { label: `Network TX (${netPlot.unit})`, color: CHART_SERIES[1] },
                                        ]}
                                    />
                                )
                            )}
                            {availableNics.length === 0 && (
                                <p className="mt-2 text-[11px] text-theme-muted">
                                    Per-interface breakdown is unavailable for this telemetry snapshot (showing aggregate RX / TX).
                                </p>
                            )}
                        </>
                    ) : (
                        <EmptyState
                            icon={<Network className="h-6 w-6" />}
                            title="Network throughput unavailable"
                            message={
                                activeNic === ALL_NICS_SCOPE
                                    ? 'Metric unavailable (node_exporter network series not detected).'
                                    : `Metric unavailable (no samples recorded for interface ${activeNic} in ${scopeLabel}).`
                            }
                        />
                    )}
                </ChartContainer>

                {/* Chart 4: Host & Accelerator Power */}
                <ChartContainer
                    title={`Host & ${accelLabel} Power`}
                    subtitle={
                        isAllNodes
                            ? `Comparison of host node power vs serving ${accelLabel.toLowerCase()} power (W)`
                            : `Host vs ${accelLabel.toLowerCase()} power (W) on ${activeNode}`
                    }
                    help={`Compares full-node host power against serving ${accelLabel.toLowerCase()} power draw (Watts) over elapsed time. Helps distinguish accelerator inference draw from CPU, memory, and chassis overhead.`}
                    helpFormula="Host_Power(t) (W) vs Accel_Power(t) (W) = Σ_devices Device_Power_W(t)"
                    helpAlign="end"
                >
                    {(hasHostPowerSeries || hasAccelPowerSeries) ? (
                        <>
                            <div className="h-56 w-full">
                                <ResponsiveContainer width="100%" height="100%">
                                    <LineChart data={activeSeries} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
                                        <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 3" vertical={false} />
                                        <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tickFormatter={(v) => `${formatNumber(v)}s`} tick={AXIS_TICK} stroke="var(--border-color)" />
                                        <YAxis width={56} tickFormatter={formatAxisTick} tick={AXIS_TICK} stroke="var(--border-color)" label={{ value: 'W', angle: -90, position: 'insideLeft', fill: 'var(--text-muted)', fontSize: 10 }} />
                                        <Tooltip cursor={LINE_TOOLTIP_CURSOR} content={<UnitTooltip unit="W" />} />
                                        {hasHostPowerSeries && (
                                            <Line type="monotone" dataKey="host_power_w" name="Host Power" stroke={CHART_SERIES[1]} strokeWidth={2} connectNulls dot={sparseDotProp(activeSeries, 'host_power_w', CHART_SERIES[1])} isAnimationActive={false} />
                                        )}
                                        {hasAccelPowerSeries && (
                                            <Line type="monotone" dataKey="accel_power_w" name={`${accelLabel} Power`} stroke={CHART_SERIES[0]} strokeWidth={2} connectNulls dot={sparseDotProp(activeSeries, 'accel_power_w', CHART_SERIES[0])} isAnimationActive={false} />
                                        )}
                                    </LineChart>
                                </ResponsiveContainer>
                            </div>
                            {(hasHostPowerSeries && hasAccelPowerSeries) && (
                                <ChartLegend
                                    className="mt-3"
                                    entries={[
                                        { label: 'Host Power (W)', color: CHART_SERIES[1] },
                                        { label: `${accelLabel} Power (W)`, color: CHART_SERIES[0] },
                                    ]}
                                />
                            )}
                            {!hasHostPowerSeries && (
                                <p className="mt-3 text-xs text-theme-muted">
                                    Metric unavailable (host power exporter such as IPMI / RAPL / Kepler not detected; showing serving {accelLabel.toLowerCase()} power).
                                </p>
                            )}
                        </>
                    ) : (
                        <EmptyState
                            icon={<Zap className="h-6 w-6" />}
                            title="Power telemetry unavailable"
                            message="Metric unavailable (node_exporter / host power exporter not detected)."
                        />
                    )}
                </ChartContainer>
            </div>
        </div>
    );
}

export default SystemTab;
