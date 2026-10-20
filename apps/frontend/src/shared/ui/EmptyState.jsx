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

const ICON_TONES = {
    info: 'border-cyan-500/30 bg-cyan-500/10 text-cyan-400',
    success: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-500 dark:text-emerald-300',
};

const ICON_GLOWS = {
    info: 'bg-cyan-500/30',
    success: 'bg-emerald-500/40',
};

const ICON_STATUS_COLORS = {
    info: 'text-cyan-400',
    success: 'text-emerald-500 dark:text-emerald-300',
};

export function EmptyState({ icon, title, message, action, className, tone = 'info', variant = 'default' }) {
    if (variant === 'status') {
        return (
            <div className={cn('relative flex flex-col items-center justify-center px-6 py-10 text-center', className)}>
                {icon && (
                    <div className={cn('relative mb-5 flex h-24 w-24 items-center justify-center', ICON_STATUS_COLORS[tone])}>
                        <div className={cn('status-glow-pulse absolute inset-0 rounded-full blur-3xl', ICON_GLOWS[tone])} aria-hidden="true" />
                        <div className="relative">{icon}</div>
                    </div>
                )}
                {title && <div className="text-[15px] font-semibold tracking-tight text-theme-text">{title}</div>}
                {message && <p className="mt-2 max-w-md text-[13px] leading-6 text-theme-muted">{message}</p>}
                {action && <div className="mt-5">{action}</div>}
            </div>
        );
    }
    return (
        <div className={cn('flex flex-col items-center justify-center text-center gap-2 py-12 px-6', className)}>
            {icon && (
                <div className={cn('mb-2 flex h-14 w-14 items-center justify-center rounded-full border', ICON_TONES[tone])}>
                    {icon}
                </div>
            )}
            {title && <div className="text-sm font-bold text-theme-text">{title}</div>}
            {message && <p className="text-xs text-theme-muted max-w-sm leading-relaxed">{message}</p>}
            {action && <div className="mt-3">{action}</div>}
        </div>
    );
}
