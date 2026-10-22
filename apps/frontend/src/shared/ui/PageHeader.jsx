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

import React, { createContext, useContext } from 'react';
import { createPortal } from 'react-dom';
import { useClipboard } from '../lib/useClipboard';
import { ArrowLeft, Menu, Share2 } from 'lucide-react';
import { cn } from '../lib/cn';
import { Button } from './Button';

const PageHeadingTarget = createContext(null);

export function PageHeadingProvider({ target, children }) {
    return <PageHeadingTarget.Provider value={target}>{children}</PageHeadingTarget.Provider>;
}

// Only the heading moves into the app bar; page actions keep their original owners.
export function PageHeading({ title, subtitle, badge, icon: Icon }) {
    const target = useContext(PageHeadingTarget);
    const heading = (
        <div className="flex min-w-0 items-center gap-3">
            {Icon && (
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-sky-500/10 text-sky-600 dark:text-sky-400">
                    <Icon className="h-4 w-4" aria-hidden="true" />
                </div>
            )}
            <div className="min-w-0">
                <div className="flex min-w-0 items-center gap-2">
                    {title && <h1 className="truncate text-base font-bold text-theme-text sm:text-lg" title={typeof title === 'string' ? title : undefined}>{title}</h1>}
                    {badge}
                </div>
                {subtitle && <p className="truncate text-xs text-theme-muted" title={typeof subtitle === 'string' ? subtitle : undefined}>{subtitle}</p>}
            </div>
        </div>
    );
    return target ? createPortal(heading, target) : heading;
}

export function PageHeader({
    title,
    subtitle,
    badge,
    icon,
    heading,
    onNavigateBack,
    onToggleMobileNav,
    actions,
    className,
}) {
    const target = useContext(PageHeadingTarget);
    const pageHeading = heading || <PageHeading title={title} subtitle={subtitle} badge={badge} icon={icon} />;
    if (target) {
        return (
            <>
                {pageHeading}
                {(onNavigateBack || actions) && (
                    <div className={cn('flex min-w-0 flex-wrap items-center justify-between gap-3', className)}>
                        {onNavigateBack && (
                            <Button variant="ghost" size="icon" onClick={onNavigateBack} aria-label="Back">
                                <ArrowLeft className="h-5 w-5" />
                            </Button>
                        )}
                        {actions && <div className="ml-auto flex min-w-0 max-w-full flex-wrap items-center gap-2">{actions}</div>}
                    </div>
                )}
            </>
        );
    }
    return (
        <header
            className={cn(
                'sticky top-0 z-40 w-full border-b border-theme-border bg-theme-bg/80 backdrop-blur-xl',
                'flex items-center justify-between gap-4 px-4 sm:px-6 py-3',
                className
            )}
        >
            <div className="flex min-w-0 flex-1 items-center gap-2">
                {onToggleMobileNav && (
                    <button
                        onClick={onToggleMobileNav}
                        aria-label="Toggle navigation"
                        className="p-1.5 rounded-lg hover:bg-slate-200/60 dark:hover:bg-slate-800 text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white transition-colors md:hidden"
                    >
                        <Menu className="h-5 w-5" />
                    </button>
                )}
                {onNavigateBack && (
                    <button
                        onClick={onNavigateBack}
                        aria-label="Back"
                        className="p-1.5 rounded-lg hover:bg-slate-200/60 dark:hover:bg-slate-800 text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-white transition-colors"
                    >
                        <ArrowLeft className="h-5 w-5" />
                    </button>
                )}
                <div className="min-w-0 flex-1">{pageHeading}</div>
            </div>
            {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
        </header>
    );
}

// "Share link" button with the built-in "Link copied!" toast used across dashboards.
// Pass getUrl to share a constructed deep link instead of the current location.
export function ShareLinkButton({ label = 'Share link', getUrl, className }) {
    const { copied, copy } = useClipboard(2000);
    const handleShare = () => copy(getUrl ? getUrl() : window.location.href);

    return (
        <Button variant="secondary" size="sm" onClick={handleShare} className={cn('relative', className)}>
            <Share2 className="w-3.5 h-3.5" />
            <span>{label}</span>
            {copied && (
                <span className="absolute -bottom-10 right-0 bg-emerald-600 text-white text-xs font-bold px-3 py-1.5 rounded shadow-lg z-50 whitespace-nowrap">
                    Link copied!
                </span>
            )}
        </Button>
    );
}
