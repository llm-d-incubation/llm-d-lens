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

import React from 'react';
import { cn } from '../../../../shared/lib/cn';
import { HelpTooltip } from '../../../../shared/ui';

// eslint-disable-next-line react-refresh/only-export-components
export function formatTaskPacing(task, telemetryOrManifest = null) {
    const manifest = telemetryOrManifest?.run_manifest
        || (telemetryOrManifest && (
            telemetryOrManifest.backend
            || telemetryOrManifest.replay
            || telemetryOrManifest.dataset
        ) ? telemetryOrManifest : null);
    const sim = task?.simulation || {};
    const snapshot = task?.config_snapshot || {};
    const trace = task?.prompt?.trace || {};
    const backend = manifest?.backend || sim.backend || task?.backend || task?.result?.backend || '';
    // Defensive historical compatibility: legacy trace-replayer tasks always ran on fixed schedule.
    if (backend === 'trace-replayer' || backend === 'trace_replayer') {
        return {
            isFixedSchedule: true,
            concurrency: null,
            label: 'Fixed Schedule',
        };
    }

    const traceFormat = manifest?.dataset?.format
        || trace.format
        || task?.trace_format
        || snapshot?.trace_format
        || '';

    const rawConcurrency = sim.backend_options?.concurrency
        ?? task?.backend_options?.concurrency
        ?? manifest?.replay?.backend_options?.concurrency
        ?? manifest?.replay?.concurrency
        ?? snapshot?.backend_options?.concurrency
        ?? snapshot?.concurrency;
    const hasConcurrency = rawConcurrency !== null
        && rawConcurrency !== undefined
        && rawConcurrency !== ''
        && Number.isFinite(Number(rawConcurrency));
    const concurrency = hasConcurrency ? Number(rawConcurrency) : null;

    const rawFixedSchedule = manifest?.replay?.fixed_schedule
        ?? trace.fixed_schedule
        ?? task?.fixed_schedule
        ?? snapshot?.fixed_schedule;

    if (traceFormat === 'weka_public_dataset' || rawFixedSchedule === false || hasConcurrency) {
        const slots = concurrency ?? 1;
        return {
            isFixedSchedule: false,
            concurrency: slots,
            label: `Concurrency (${slots})`,
        };
    }

    if (rawFixedSchedule === true || backend || task || manifest) {
        return {
            isFixedSchedule: true,
            concurrency: null,
            label: 'Fixed Schedule',
        };
    }

    return {
        isFixedSchedule: true,
        concurrency: null,
        label: null,
    };
}

export function ConfigurationGroup({ title, items, help, helpFormula }) {
    const visibleItems = items.filter(([, value]) => value !== null && value !== undefined && value !== '');
    if (!visibleItems.length) return null;
    const hasGroupHelp = Boolean(help || helpFormula);
    return (
        <section className="rounded-lg border border-theme-border bg-theme-bg/50 p-4">
            <div className="mb-3 flex items-center gap-1.5">
                <h4 className="text-[10px] font-bold uppercase tracking-wider text-theme-muted">{title}</h4>
                {hasGroupHelp && (
                    <HelpTooltip label={`${title} help`} align="start" iconSize={12} formula={helpFormula}>
                        {help}
                    </HelpTooltip>
                )}
            </div>
            <dl className="grid gap-x-5 gap-y-3 text-xs sm:grid-cols-2">
                {visibleItems.map(([label, value, itemHelp, itemFormula]) => {
                    const hasItemHelp = Boolean(itemHelp || itemFormula);
                    return (
                        <div key={label} className={cn(label === 'Trace path' || label === 'Endpoint' ? 'sm:col-span-2' : '')}>
                            <dt className={cn('text-theme-muted', hasItemHelp && 'flex items-center gap-1')}>
                                <span>{label}</span>
                                {hasItemHelp && (
                                    <HelpTooltip label={`${label} help`} align="start" iconSize={12} formula={itemFormula}>
                                        {itemHelp}
                                    </HelpTooltip>
                                )}
                            </dt>
                            <dd className="mt-0.5 break-all text-theme-text">{value}</dd>
                        </div>
                    );
                })}
            </dl>
        </section>
    );
}
