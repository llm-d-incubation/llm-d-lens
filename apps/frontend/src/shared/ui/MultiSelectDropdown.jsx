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

import React, { useState, useRef, useEffect, useMemo } from 'react';
import { ChevronDown, Check, Search } from 'lucide-react';
import { Button } from './Button';
import { Input } from './FormControls';
import { cn } from '../lib/cn';

export const MultiSelectDropdown = ({ label, options, selected = new Set(), onChange, counts, formatLabel, labelSuffix, emptyLabel = 'All', clearLabel, className = '', searchable = false, searchPlaceholder = 'Search…', disabledOptions, optionDescription, optionGroup, inlineMenu = false, defaultOpen = false }) => {
    const [isOpen, setIsOpen] = useState(defaultOpen);
    const [query, setQuery] = useState('');
    const containerRef = useRef(null);

    useEffect(() => {
        const handleClickOutside = (event) => {
            if (containerRef.current && !containerRef.current.contains(event.target)) {
                setIsOpen(false);
            }
        };
        document.addEventListener('mousedown', handleClickOutside);
        return () => document.removeEventListener('mousedown', handleClickOutside);
    }, []);

    useEffect(() => {
        if (!isOpen) setQuery('');
    }, [isOpen]);

    const safeSelected = selected || new Set();
    const selectedCount = safeSelected.size;
    const hasCounts = counts !== undefined && counts !== null;
    const visibleOptions = useMemo(() => {
        const needle = query.trim().toLowerCase();
        if (!needle) return options;
        return options.filter((opt) => {
            const label = formatLabel ? formatLabel(opt) : opt;
            return String(label).toLowerCase().includes(needle) || String(opt).toLowerCase().includes(needle);
        });
    }, [options, query, formatLabel]);

    return (
        <div className="relative" ref={containerRef}>
            <Button variant="ghost"
                type="button"
                onClick={() => setIsOpen(!isOpen)}
                aria-expanded={isOpen}
                className={cn('w-full border border-theme-border text-theme-text text-xs rounded-xl px-3 py-2 flex items-center justify-between', className)}
                title={`${label}: ${selectedCount > 0 ? [...safeSelected].join(', ') : 'All'}`}
            >
                <div className="flex items-center gap-2 truncate pr-2">
                    <span className="font-semibold text-theme-muted">{label}</span>
                    {labelSuffix}
                    <span className="truncate text-theme-text">
                        {selectedCount === 0 ? emptyLabel : `${selectedCount} selected`}
                    </span>
                </div>
                <ChevronDown size={12} className={cn('text-theme-muted transition-transform', isOpen && 'rotate-180')} />
            </Button>

            {isOpen && (
                <div role="group" aria-label={`${label} options`} className={cn('mt-2 max-h-80 overflow-y-auto bg-theme-card border border-theme-border rounded-xl shadow-2xl z-[100] p-2.5 space-y-1.5', inlineMenu ? 'relative w-full' : 'absolute top-full left-0 w-72')}>
                    {searchable && (
                        <div className="relative mb-1">
                            <Search size={12} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-500" />
                            <Input aria-label={`Search ${label}`}
                                type="text"
                                value={query}
                                onChange={(event) => setQuery(event.target.value)}
                                placeholder={searchPlaceholder}
                                autoFocus
                                className="w-full py-1.5 pl-7 pr-2 text-xs"
                            />
                        </div>
                    )}
                    <Button type="button" variant="ghost" className="w-full flex items-center gap-2 text-xs"
                        onClick={() => { onChange(''); setIsOpen(false); }}
                    >
                         <span className="text-xs">{clearLabel || `All ${label}`}</span>
                         <span className="text-[10px] text-slate-500 ml-auto font-mono">{options.length}</span>
                    </Button>
                    
                    <div className="h-px bg-slate-800/30 my-1.5 mx-1" />

                    {visibleOptions.length === 0 && (
                        <p className="px-2.5 py-2 text-xs text-slate-500">No matches</p>
                    )}
                    {visibleOptions.map((opt, index) => {
                        const count = (counts && counts[opt]) || 0;
                        const isSelected = safeSelected.has(opt);
                        const description = optionDescription?.(opt);
                        return (
                            <React.Fragment key={opt}>
                            {optionGroup && (index === 0 || optionGroup(opt) !== optionGroup(visibleOptions[index - 1])) && <p className="text-xs font-semibold text-theme-muted">{optionGroup(opt)}</p>}
                            <Button type="button" variant="ghost" role="checkbox" aria-checked={isSelected} disabled={disabledOptions?.has(opt)} aria-label={formatLabel ? formatLabel(opt) : opt}
                                className={cn('w-full h-auto flex items-start gap-2 px-2.5 py-2 rounded-lg text-left whitespace-normal', hasCounts && count === 0 && 'opacity-45', isSelected && 'bg-theme-bg font-semibold')}
                                onClick={() => onChange(opt)}
                                title={description || (formatLabel ? formatLabel(opt) : opt)}
                            >
                                 <span className="w-3.5 h-3.5 shrink-0 rounded border border-theme-border flex items-center justify-center">
                                    {isSelected && <Check size={10} strokeWidth={3} />}
                                 </span>
                                 <span className="text-xs flex-1 min-w-0 text-theme-text">
                                     {formatLabel ? formatLabel(opt) : opt}
                                     {description && <small className="block mt-1 font-normal text-theme-muted">{description}</small>}
                                 </span>
                                 {hasCounts && <span className="text-[10px] text-slate-500 dark:text-slate-400 font-mono ml-auto">{count}</span>}
                            </Button></React.Fragment>
                        );
                    })}
                </div>
            )}
        </div>
    );
};
