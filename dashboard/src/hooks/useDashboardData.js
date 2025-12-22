import { useState, useEffect, useCallback } from 'react';

const API_BASE_URL = 'http://127.0.0.1:6942/api';

export const useDashboardData = () => {
    const [sources, setSources] = useState([]);
    const [dailyStats, setDailyStats] = useState(null);
    const [timeseriesData, setTimeseriesData] = useState([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);

    useEffect(() => {
        const fetchSources = async () => {
            try {
                setLoading(true);
                const response = await fetch(`${API_BASE_URL}/sources`);
                if (!response.ok) throw new Error('Failed to fetch source list');
                const data = await response.json();
                setSources(data);
            } catch (err) {
                setError('Could not connect to the backend. Please ensure the proxy service is running.');
                console.error(err);
            } finally {
                setLoading(false);
            }
        };
        fetchSources();
    }, []);

    const fetchData = useCallback(async (source, date, interval) => {
        if (!source || !date) return;

        setLoading(true);
        setError(null);

        try {
            const dailyRes = await fetch(`${API_BASE_URL}/stats/daily?source=${source}&date=${date}`);
            if (!dailyRes.ok) throw new Error(`Failed to fetch daily stats for ${source}`);
            const dailyData = await dailyRes.json();
            setDailyStats(dailyData);

            const timeseriesRes = await fetch(`${API_BASE_URL}/stats/timeseries?source=${source}&date=${date}&interval=${interval}`);
            if (!timeseriesRes.ok) throw new Error(`Failed to fetch timeseries data for ${source}`);
            const tsData = await timeseriesRes.json();
            setTimeseriesData(tsData);
        } catch (err) {
            setError(err.message);
            console.error(err);
        } finally {
            setLoading(false);
        }
    }, []);

    return {
        sources,
        dailyStats,
        setDailyStats,
        timeseriesData,
        setTimeseriesData,
        loading,
        error,
        setError,
        fetchData
    };
};
