import { useEffect, useState } from 'react';
import { getHardwareProfiles } from '../components/ClusterMonitoringStack/hardwareProfilesBackend.js';

export function useHardwareProfiles() {
    const [profiles, setProfiles] = useState([]);
    useEffect(() => {
        const controller = new AbortController();
        getHardwareProfiles({ signal: controller.signal }).then(payload => {
            if (!controller.signal.aborted) setProfiles(payload.profiles || []);
        }).catch(() => {});
        return () => controller.abort();
    }, []);
    return profiles;
}
