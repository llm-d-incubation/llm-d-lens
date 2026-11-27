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
import { cn } from '../../lib/cn';
import { HelpTooltip } from '../FormControls';

// Chart panel shell. Successor to common/ChartCard, theme-aware. In dark mode
// it keeps ChartCard's translucent "glass" surface so the page's dotted grid
// and glow blobs bleed through. Put filter controls in `actions`; keep them in
// one row above the plot.
export function ChartContainer({ title, subtitle, help, helpFormula, formula, helpAlign = 'start', actions, className, children }) {
    const resolvedFormula = helpFormula ?? formula;
    const hasHelp = Boolean(help || resolvedFormula);
    return (
        <div
            className={cn(
                'relative hover:z-30 focus-within:z-30',
                'bg-theme-card dark:bg-slate-900/50 border border-theme-border dark:border-slate-800/80',
                'backdrop-blur-xl rounded-2xl p-6 shadow-2xl transition-all',
                className
            )}
        >
            {(title || hasHelp || actions) && (
                <div className="flex items-start justify-between gap-4 mb-5">
                    <div className="min-w-0">
                        {(title || hasHelp) && (
                            <div className="flex items-center gap-1.5">
                                {title && (
                                    <h3 className="text-slate-600 dark:text-slate-300 text-xs font-bold uppercase tracking-wider font-mono">
                                        {title}
                                    </h3>
                                )}
                                {hasHelp && (
                                    <HelpTooltip
                                        label={`${typeof title === 'string' ? title : 'Chart'} help`}
                                        align={helpAlign}
                                        iconSize={13}
                                        formula={resolvedFormula}
                                    >
                                        {help}
                                    </HelpTooltip>
                                )}
                            </div>
                        )}
                        {subtitle && <p className="text-[11px] text-theme-muted mt-1">{subtitle}</p>}
                    </div>
                    {actions && <div className="flex items-center gap-2 shrink-0 flex-wrap justify-end">{actions}</div>}
                </div>
            )}
            {children}
        </div>
    );
}
