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
import { Menu } from 'lucide-react';
import { cn } from '../lib/cn';

// The canonical header for a workspace module (Deployments, Evaluation,
// Simulation, Cluster, Observability): the module's own icon, its name, and a
// one-line summary of what the module does. The icon tile is uniformly sky blue
// (the SectionLabel "sky" tone) so every module header reads the same. Deliberately carries no product branding —
// the left navigation already establishes that context.
export function ModuleHeader({
    icon: Icon,
    title,
    description,
    badge,
    actions,
    onToggleMobileNav,
    className,
}) {
    return (
        <header
            className={cn(
                'flex flex-wrap items-start justify-between gap-3 border-b border-slate-200/80 pb-4 dark:border-slate-800/70',
                className
            )}
        >
            <div className="flex min-w-0 items-start gap-3">
                {onToggleMobileNav && (
                    <button
                        type="button"
                        onClick={onToggleMobileNav}
                        aria-label="Toggle navigation"
                        className="rounded-lg p-1.5 text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-white md:hidden"
                    >
                        <Menu className="h-5 w-5" />
                    </button>
                )}
                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-[#037ffc] bg-[#037ffc] text-white dark:border-sky-500/25 dark:bg-sky-500/10 dark:text-sky-400">
                    {Icon && <Icon className="h-5 w-5" aria-hidden="true" />}
                </div>
                <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                        <h1 className="text-xl font-bold tracking-tight text-slate-900 dark:text-white">{title}</h1>
                        {badge && (
                            <span className="module-header-badge rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-300">
                                {badge}
                            </span>
                        )}
                    </div>
                    {description && <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{description}</p>}
                </div>
            </div>
            {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
    );
}
