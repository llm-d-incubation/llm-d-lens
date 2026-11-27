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
import { HelpTooltip, Panel } from '../../../../shared/ui';
import { formatMilliseconds, metricDistribution } from '../../model/presentation';

const STATISTIC_HELP = {
    Mean: {
        text: 'Arithmetic average latency in milliseconds across all sampled requests. Sensitive to tail outliers compared with P50.',
        formula: 'Mean = (1 / N) Σ x_i',
    },
    P50: {
        text: 'Median (50th percentile) latency in milliseconds; half of sampled requests completed at or below this value.',
        formula: 'P50 = Quantile_0.50(sorted(x))',
    },
    P90: {
        text: '90th percentile latency in milliseconds; 90% of sampled requests finished within this threshold.',
        formula: 'P90 = Quantile_0.90(sorted(x))',
    },
    P95: {
        text: '95th percentile latency in milliseconds; captures upper-tail latency experienced by the slowest 5% of requests.',
        formula: 'P95 = Quantile_0.95(sorted(x))',
    },
    P99: {
        text: '99th percentile latency in milliseconds; highlights severe tail outliers affecting the slowest 1% of requests.',
        formula: 'P99 = Quantile_0.99(sorted(x))',
    },
    Min: {
        text: 'Minimum observed latency in milliseconds across sampled requests.',
        formula: 'Min = min_i(x_i)',
    },
    Max: {
        text: 'Maximum (worst-case) observed latency in milliseconds across sampled requests.',
        formula: 'Max = max_i(x_i)',
    },
};

export function StatisticsPanel({ title, values, help, helpFormula }) {
    const rows = metricDistribution(values);
    return (
        <Panel title={title} help={help} helpFormula={helpFormula}>
            {rows.length ? (
                <dl className="divide-y divide-slate-800">
                    {rows.map(([label, value]) => (
                        <div key={label} className="flex items-center justify-between py-2 text-xs">
                            <dt className="flex items-center gap-1 text-slate-500">
                                <span>{label}</span>
                                {STATISTIC_HELP[label] && (
                                    <HelpTooltip label={`${label} help`} align="start" iconSize={12}>
                                        {STATISTIC_HELP[label]}
                                    </HelpTooltip>
                                )}
                            </dt>
                            <dd className="font-mono font-semibold text-slate-200">{formatMilliseconds(value)}</dd>
                        </div>
                    ))}
                </dl>
            ) : <p className="text-xs text-slate-500">No statistics are available yet.</p>}
        </Panel>
    );
}
