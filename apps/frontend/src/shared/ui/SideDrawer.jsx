import React, { useEffect, useId, useRef } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { cn } from '../lib/cn';

const FOCUSABLE_SELECTOR = [
    'button:not([disabled])',
    'a[href]',
    'input:not([disabled])',
    'select:not([disabled])',
    'textarea:not([disabled])',
    'summary',
    '[tabindex="0"]',
].join(', ');

/** An overlay inspector: slides in from the right edge at full viewport height. */
export function SideDrawer({
    isOpen = true,
    title,
    subtitle,
    icon = null,
    headerMeta = null,
    headerActions = null,
    footer = null,
    onClose,
    closeLabel = 'Close inspector',
    widthClassName = 'sm:w-[480px] sm:max-w-[85vw]',
    bodyClassName,
    className,
    children,
}) {
    const panel = useRef(null);
    const close = useRef(onClose);
    const titleId = useId();
    useEffect(() => {
        close.current = onClose;
    }, [onClose]);
    useEffect(() => {
        if (!isOpen || typeof document === 'undefined') return undefined;
        const previousFocus = document.activeElement;
        const previousOverflow = document.body.style.overflow;
        document.body.style.overflow = 'hidden';
        panel.current?.focus();
        const onKey = (event) => {
            if (event.key === 'Escape') {
                event.preventDefault();
                event.stopPropagation();
                close.current?.();
            }
            if (event.key !== 'Tab') return;
            event.stopPropagation();
            const controls = [...(panel.current?.querySelectorAll(FOCUSABLE_SELECTOR) || [])].filter(
                (el) => el.getClientRects().length,
            );
            const first = controls[0];
            const last = controls[controls.length - 1];
            if (!first) {
                event.preventDefault();
                panel.current?.focus();
                return;
            }
            if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) {
                event.preventDefault();
                last.focus();
            }
            if (!event.shiftKey && (document.activeElement === last || !panel.current?.contains(document.activeElement))) {
                event.preventDefault();
                first.focus();
            }
        };
        window.addEventListener('keydown', onKey, true);
        return () => {
            document.body.style.overflow = previousOverflow;
            window.removeEventListener('keydown', onKey, true);
            if (previousFocus?.isConnected) previousFocus.focus?.();
        };
    }, [isOpen]);

    if (!isOpen || typeof document === 'undefined') return null;

    const resolvedSubtitle = subtitle === undefined ? 'Recorded evidence' : subtitle;

    return createPortal(
        <div className="fixed inset-0 z-[210]">
            <div aria-hidden="true" onClick={onClose} className="side-drawer-overlay absolute inset-0" />
            <section
                ref={panel}
                tabIndex={-1}
                role="dialog"
                aria-modal="true"
                aria-labelledby={titleId}
                className={cn(
                    'benchmark-inspector-drawer absolute inset-y-0 right-0 flex h-dvh w-full flex-col border-l border-theme-border bg-theme-card text-theme-text shadow-2xl shadow-black/20 dark:shadow-black/50 outline-none',
                    widthClassName,
                    className,
                )}
            >
                <header className="flex shrink-0 items-start justify-between gap-4 border-b border-theme-border px-6 py-5">
                    <div className="min-w-0 flex-1">
                        {resolvedSubtitle ? (
                            <p className="mb-2 text-[10px] font-semibold uppercase tracking-widest text-sky-600 dark:text-sky-300">
                                {resolvedSubtitle}
                            </p>
                        ) : null}
                        <h2
                            id={titleId}
                            className={cn(
                                'break-words text-base font-semibold text-theme-text',
                                icon && 'flex items-center gap-2',
                            )}
                        >
                            {icon ? <span className="shrink-0">{icon}</span> : null}
                            {icon ? <span className="min-w-0">{title}</span> : title}
                        </h2>
                        {headerMeta ? <div className="mt-2">{headerMeta}</div> : null}
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                        {headerActions}
                        <button
                            type="button"
                            onClick={onClose}
                            aria-label={closeLabel}
                            className="rounded-lg p-2 text-theme-muted hover:bg-theme-bg hover:text-theme-text focus-visible:outline-sky-400"
                        >
                            <X size={18} />
                        </button>
                    </div>
                </header>
                <div className={cn('min-h-0 flex-1 overflow-y-auto overscroll-contain p-6', bodyClassName)}>
                    {children}
                </div>
                {footer ? (
                    <footer className="shrink-0 border-t border-theme-border px-6 py-4">
                        {footer}
                    </footer>
                ) : null}
            </section>
        </div>,
        document.body,
    );
}
