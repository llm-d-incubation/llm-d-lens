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
import { cn } from '../lib/cn';
import { Button } from './Button';

const PADDING = {
    none: 'p-0',
    sm: 'p-4',
    md: 'p-6',
};

const TONES = {
    neutral: '',
    subtle: 'bg-theme-card/40 bg-linear-to-br from-slate-100/70 to-transparent dark:from-slate-900/50',
    soft: 'border-cyan-500/15 bg-linear-to-br from-slate-100/80 via-slate-50/70 to-cyan-50/15 dark:from-slate-900/80 dark:via-slate-950/70 dark:to-cyan-950/15',
    inset: 'bg-linear-to-br from-slate-50/70 to-cyan-50/10 dark:from-slate-950/70 dark:to-cyan-950/10',
};

// The standard card surface. Replaces hand-rolled
// "bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700" shells.
export function Panel({ title, actions, padding = 'md', tone = 'neutral', className, children, ...props }) {
    return (
        <div
            className={cn(
                'bg-theme-card border border-theme-border rounded-xl shadow-lg transition-colors',
                TONES[tone],
                PADDING[padding],
                className
            )}
            {...props}
        >
            {(title || actions) && (
                <div className={cn('flex items-center justify-between gap-4 mb-4', padding === 'none' && 'p-4 mb-0 border-b border-theme-border')}>
                    {title && (
                        <h3 className="text-sm font-bold text-theme-text">{title}</h3>
                    )}
                    {actions && <div className="flex items-center gap-2">{actions}</div>}
                </div>
            )}
            {children}
        </div>
    );
}

/** Compact workspace bar: back action, identity (icon, title, badge, subtitle) and primary actions on one line. */
export function WorkspaceHeader({ title, subtitle, icon, badge, actions, backAction }) {
    return <header className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-theme-border bg-theme-card px-4 py-3 shadow-sm">
        <div className="flex min-w-0 items-center gap-3">
            {backAction && <div className="flex items-center border-r border-theme-border pr-3">{backAction}</div>}
            {icon && <span className="grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-emerald-500/10 text-emerald-600 dark:text-emerald-300" aria-hidden="true">{icon}</span>}
            <div className="min-w-0">
                <div className="flex min-w-0 flex-wrap items-center gap-2"><h2 className="truncate text-lg font-semibold leading-tight text-theme-text">{title}</h2>{badge}</div>
                {subtitle && <div className="mt-1 flex min-w-0 items-center gap-1.5 truncate text-xs text-theme-muted">{subtitle}</div>}
            </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">{actions}</div>
    </header>;
}

const STRIP_TONES = {
    neutral: 'bg-slate-500/10 text-slate-500 dark:text-slate-400',
    brand: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-300',
    success: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-300',
    info: 'bg-sky-500/10 text-sky-600 dark:text-sky-300',
    warning: 'bg-amber-500/10 text-amber-600 dark:text-amber-300',
    danger: 'bg-red-500/10 text-red-600 dark:text-red-300',
    violet: 'bg-violet-500/10 text-violet-600 dark:text-violet-300',
};

/** Compact context row; interactive facts open their existing editor or review. Items may carry an `icon` and `tone`. */
export function SummaryStrip({ items, ...props }) {
    return <div className="grid grid-cols-2 overflow-hidden rounded-xl border border-theme-border bg-theme-card md:grid-cols-4" {...props}>
        {items.map(item => <div key={item.label} className="flex min-w-0 items-start gap-3 border-b border-r border-theme-border p-4 last:border-r-0 md:border-b-0">
            {item.icon && <span className={cn('grid h-9 w-9 shrink-0 place-items-center rounded-lg', STRIP_TONES[item.tone] || STRIP_TONES.neutral)} aria-hidden="true"><item.icon size={17} /></span>}
            <div className="min-w-0">
                <div className="text-xs text-theme-muted">{item.label}</div>
                {item.onClick ? <Button variant="link" className="mt-1 h-auto max-w-full justify-start whitespace-normal p-0 text-left text-sm font-semibold" onClick={item.onClick} disabled={item.disabled}>{item.value}</Button> : <div className="mt-1 break-words text-sm font-semibold">{item.value}</div>}
                {item.detail && <div className="mt-1 truncate text-xs text-theme-muted" title={typeof item.detail === 'string' ? item.detail : undefined}>{item.detail}</div>}
            </div>
        </div>)}
    </div>;
}
