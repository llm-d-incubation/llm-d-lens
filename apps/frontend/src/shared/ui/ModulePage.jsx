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

// Workspace content sits below the app bar with balanced responsive gutters.
// Pair with ModuleHeader as the first child.
export function ModulePage({ children, className, contentClassName }) {
    return (
        <div
            className={cn(
                'min-h-[var(--workspace-height,100dvh)] w-full shrink-0 bg-theme-bg text-theme-text font-sans antialiased relative overflow-x-hidden',
                className
            )}
        >
            <main className={cn('relative z-10 w-full px-4 py-6 sm:px-6 lg:px-12 lg:py-10', contentClassName)}>{children}</main>
        </div>
    );
}
