import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { AuthContext } from './AuthContext';
import { fetchSession, login as loginRequest, logout as logoutRequest, refreshSession } from './authClient';
import { canAccessView, hasPermission } from './permissions';
import { setAuthToken, setUnauthorizedHandler } from '../../shared/api/httpClient';

/**
 * Holds the current principal and a short-lived in-memory Bearer token. The
 * long-lived refresh credential is an httpOnly cookie; on load and after a 401
 * this provider exchanges it for a new access token.
 */
export function AuthProvider({ children }) {
    const [status, setStatus] = useState('loading');
    const [principal, setPrincipal] = useState(null);
    const [accessToken, setAccessTokenState] = useState('');

    const applyAccessToken = useCallback((token) => {
        const next = token || '';
        setAccessTokenState(next);
        setAuthToken(next);
    }, []);

    const clearSession = useCallback(() => {
        applyAccessToken('');
        setPrincipal(null);
        setStatus('anonymous');
    }, [applyAccessToken]);

    const refreshAccessToken = useCallback(async () => {
        try {
            const data = await refreshSession();
            applyAccessToken(data.accessToken);
            setPrincipal(data.principal);
            setStatus('authenticated');
            return data;
        } catch {
            return null;
        }
    }, [applyAccessToken]);

    const refresh = useCallback(async () => {
        const refreshed = await refreshAccessToken();
        if (refreshed) return refreshed.principal;
        try {
            const data = await fetchSession();
            applyAccessToken('');
            setPrincipal(data);
            setStatus('authenticated');
            return data;
        } catch {
            clearSession();
            return null;
        }
    }, [applyAccessToken, clearSession, refreshAccessToken]);

    useEffect(() => {
        refresh();
        setUnauthorizedHandler(async () => {
            if (await refreshAccessToken()) return true;
            clearSession();
            return false;
        });
        const onExpired = () => clearSession();
        window.addEventListener('prism:auth-expired', onExpired);
        return () => {
            setUnauthorizedHandler(null);
            window.removeEventListener('prism:auth-expired', onExpired);
        };
    }, [clearSession, refresh, refreshAccessToken]);

    const login = useCallback(async (username, password, remember = false) => {
        const data = await loginRequest(username, password, remember);
        applyAccessToken(data.accessToken);
        setPrincipal(data.principal);
        setStatus('authenticated');
        return data;
    }, [applyAccessToken]);

    const logout = useCallback(async () => {
        try {
            await logoutRequest();
        } finally {
            clearSession();
        }
    }, [clearSession]);

    const value = useMemo(() => {
        const permissions = principal?.permissions ?? [];
        return {
            status,
            principal,
            accessToken,
            permissions,
            can: (permission) => hasPermission(permissions, permission),
            canView: (view) => canAccessView(permissions, view),
            login,
            logout,
            refresh,
        };
    }, [status, principal, accessToken, login, logout, refresh]);

    return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
