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
import { PageHeader } from './PageHeader';

// Module titles use the app bar when available; controls stay in the content area.
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
        <PageHeader
            icon={Icon}
            title={title}
            subtitle={description}
            badge={badge && (
                <span className="module-header-badge shrink-0 rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-700 dark:text-amber-300">
                    {badge}
                </span>
            )}
            actions={actions}
            onToggleMobileNav={onToggleMobileNav}
            className={cn(
                'relative flex-wrap border-0 bg-transparent px-0 py-0 backdrop-blur-none sm:px-0',
                className
            )}
        />
    );
}
