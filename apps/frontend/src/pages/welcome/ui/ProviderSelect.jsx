import React, { useEffect, useRef, useState } from 'react';
import { Check, ChevronDown, Plus } from 'lucide-react';
import { cn } from '../../../shared/lib/cn';

function providerLabel(provider) {
    return provider.name?.trim() || provider.model || provider.id;
}

export function ProviderSelect({
    providers = [],
    value,
    onChange,
    disabled = false,
    loading = false,
    emptyLabel = 'No compatible provider',
    onAddProvider,
}) {
    const [open, setOpen] = useState(false);
    const rootRef = useRef(null);
    const triggerRef = useRef(null);
    const menuRef = useRef(null);
    const selected = providers.find((provider) => provider.id === value) || null;
    const label = selected
        ? providerLabel(selected)
        : loading
            ? 'Loading providers…'
            : providers.length
                ? 'Select provider'
                : emptyLabel;

    useEffect(() => {
        if (!open) return undefined;
        const closeOnOutsideClick = (event) => {
            if (!rootRef.current?.contains(event.target)) setOpen(false);
        };
        document.addEventListener('mousedown', closeOnOutsideClick);
        return () => document.removeEventListener('mousedown', closeOnOutsideClick);
    }, [open]);

    useEffect(() => {
        if (!open) return undefined;
        const timer = window.setTimeout(() => {
            const current = menuRef.current?.querySelector('[aria-selected="true"]');
            const first = menuRef.current?.querySelector('[role="option"]');
            const add = menuRef.current?.querySelector('[data-add-provider]');
            (current || first || add)?.focus();
        }, 0);
        return () => window.clearTimeout(timer);
    }, [open]);

    const choose = (providerId) => {
        onChange(providerId);
        setOpen(false);
        triggerRef.current?.focus();
    };

    const onTriggerKeyDown = (event) => {
        if (!disabled && ['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) {
            event.preventDefault();
            setOpen(true);
        } else if (event.key === 'Escape') {
            setOpen(false);
        }
    };

    const onMenuKeyDown = (event) => {
        const options = [...(menuRef.current?.querySelectorAll('[role="option"]') || [])];
        const index = options.indexOf(document.activeElement);
        if (event.key === 'ArrowDown') {
            event.preventDefault();
            options[(index + 1) % options.length]?.focus();
        } else if (event.key === 'ArrowUp') {
            event.preventDefault();
            options[(index - 1 + options.length) % options.length]?.focus();
        } else if (event.key === 'Escape') {
            event.preventDefault();
            setOpen(false);
            triggerRef.current?.focus();
        }
    };

    return (
        <div ref={rootRef} className="pointer-events-auto relative min-w-0">
            <button
                ref={triggerRef}
                type="button"
                disabled={disabled}
                onClick={() => setOpen((current) => !current)}
                onKeyDown={onTriggerKeyDown}
                aria-haspopup="listbox"
                aria-expanded={open}
                aria-label="AI provider"
                title={label}
                className={cn(
                    'flex max-w-[12rem] items-center gap-1 rounded px-0.5 py-1 text-xs text-slate-500 transition-colors',
                    'hover:text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/40',
                    'disabled:cursor-not-allowed disabled:opacity-50 dark:text-slate-400 dark:hover:text-slate-200',
                    open && 'text-sky-600 dark:text-sky-300',
                )}
            >
                <span className="truncate">{label}</span>
                <ChevronDown size={12} className={cn('shrink-0 transition-transform', open && 'rotate-180')} aria-hidden="true" />
            </button>
            {open && (
                <div
                    ref={menuRef}
                    onKeyDown={onMenuKeyDown}
                    className="absolute bottom-full left-0 z-[120] mb-2 max-h-64 w-72 overflow-y-auto rounded-xl border border-[#cbd5e1] bg-white/95 dark:border-sky-500/20 p-1.5 backdrop-blur-xl dark:bg-slate-900/95 dark:shadow-2xl dark:shadow-sky-500/10"
                >
                    <div role="listbox" aria-label="AI provider">
                        {providers.length === 0 ? (
                            <p className="px-3 py-2 text-xs text-theme-muted">{emptyLabel}</p>
                        ) : (
                            providers.map((provider) => {
                                const active = provider.id === value;
                                return (
                                    <button
                                        key={provider.id}
                                        type="button"
                                        role="option"
                                        aria-selected={active}
                                        onClick={() => choose(provider.id)}
                                        title={providerLabel(provider)}
                                        className={cn(
                                            'flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs transition-colors',
                                            active
                                                ? 'bg-sky-500/10 font-semibold text-sky-700 dark:text-sky-300'
                                                : 'text-slate-600 hover:bg-slate-900/5 dark:text-slate-300 dark:hover:bg-slate-800/60',
                                        )}
                                    >
                                        <span className="min-w-0 flex-1 truncate">{providerLabel(provider)}</span>
                                        {active && <Check size={13} className="shrink-0" aria-hidden="true" />}
                                    </button>
                                );
                            })
                        )}
                    </div>
                    {onAddProvider && (
                        <>
                            <div className="my-1.5 border-t border-sky-500/15" role="separator" />
                            <button
                                type="button"
                                data-add-provider
                                onClick={() => {
                                    setOpen(false);
                                    onAddProvider();
                                }}
                                className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs font-semibold text-sky-700 transition-colors hover:bg-sky-500/10 dark:text-sky-300"
                            >
                                <Plus size={13} aria-hidden="true" />
                                Add AI provider
                            </button>
                        </>
                    )}
                </div>
            )}
        </div>
    );
}
