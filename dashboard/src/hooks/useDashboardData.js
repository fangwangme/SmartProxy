import { useState, useEffect, useCallback } from 'react';

const API_BASE_URL = 'http://127.0.0.1:6942/api';
const ALL_SOURCES_OPTION = 'ALL';

export const useDashboardData = () => {
    const [sources, setSources] = useState([]);
    const [dailyStats, setDailyStats] = useState(null);
    const [timeseriesData, setTimeseriesData] = useState([]);
    const [chartSources, setChartSources] = useState([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);

    useEffect(() => {
        const fetchSources = async () => {
            try {
                setLoading(true);
                const response = await fetch(`${API_BASE_URL}/sources`);
                if (!response.ok) throw new Error('Failed to fetch source list');
                const data = await response.json();
                const normalized = Array.from(
                    new Set((data || []).filter((s) => s && s !== ALL_SOURCES_OPTION))
                );
                setSources([ALL_SOURCES_OPTION, ...normalized]);
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
            if (source === ALL_SOURCES_OPTION) {
                const requestedSources = sources.filter((s) => s !== ALL_SOURCES_OPTION);
                if (requestedSources.length === 0) {
                    setDailyStats({ total_requests: 0, total_success: 0, success_rate: 0 });
                    setTimeseriesData([]);
                    setChartSources([]);
                    return;
                }

                const settled = await Promise.allSettled(
                    requestedSources.map(async (src) => {
                        const [dailyRes, timeseriesRes] = await Promise.all([
                            fetch(`${API_BASE_URL}/stats/daily?source=${src}&date=${date}`),
                            fetch(`${API_BASE_URL}/stats/timeseries?source=${src}&date=${date}&interval=${interval}`),
                        ]);

                        if (!dailyRes.ok || !timeseriesRes.ok) {
                            throw new Error(`Failed to fetch data for ${src}`);
                        }

                        const [daily, timeseries] = await Promise.all([dailyRes.json(), timeseriesRes.json()]);
                        return { source: src, daily, timeseries };
                    })
                );

                const fulfilled = settled
                    .filter((item) => item.status === 'fulfilled')
                    .map((item) => item.value);
                const failed = settled
                    .filter((item) => item.status === 'rejected')
                    .map((item) => item.reason?.message || 'unknown source');

                if (fulfilled.length === 0) {
                    throw new Error('Failed to fetch data for all sources.');
                }

                if (failed.length > 0) {
                    setError(`Some sources failed to load: ${failed.join(', ')}`);
                }

                const activeSources = fulfilled
                    .filter((item) => (item.daily?.total_requests || 0) > 0)
                    .map((item) => item.source);
                const seriesSources = activeSources.length > 0 ? activeSources : fulfilled.map((item) => item.source);

                const totalRequests = fulfilled.reduce(
                    (sum, item) => sum + (item.daily?.total_requests || 0),
                    0
                );
                const totalSuccess = fulfilled.reduce(
                    (sum, item) => sum + (item.daily?.total_success || 0),
                    0
                );
                const successRate = totalRequests > 0 ? (totalSuccess / totalRequests) * 100 : 0;

                const timeMap = new Map();
                for (const item of fulfilled) {
                    for (const point of item.timeseries || []) {
                        if (!timeMap.has(point.time)) {
                            timeMap.set(point.time, { time: point.time, _meta: {} });
                        }
                        const row = timeMap.get(point.time);
                        row[item.source] = point.success_rate;
                        row[`${item.source}__req`] = point.total_requests;
                        row._meta[item.source] = {
                            total_requests: point.total_requests,
                            success_count: point.success_count,
                        };
                    }
                }

                const mergedTimeseries = Array.from(timeMap.values()).sort((a, b) =>
                    a.time.localeCompare(b.time)
                );
                setDailyStats({
                    total_requests: totalRequests,
                    total_success: totalSuccess,
                    success_rate: Number(successRate.toFixed(2)),
                });
                setTimeseriesData(mergedTimeseries);
                setChartSources(seriesSources);
                return;
            }

            const dailyRes = await fetch(`${API_BASE_URL}/stats/daily?source=${source}&date=${date}`);
            if (!dailyRes.ok) throw new Error(`Failed to fetch daily stats for ${source}`);
            const dailyData = await dailyRes.json();
            setDailyStats(dailyData);

            const timeseriesRes = await fetch(`${API_BASE_URL}/stats/timeseries?source=${source}&date=${date}&interval=${interval}`);
            if (!timeseriesRes.ok) throw new Error(`Failed to fetch timeseries data for ${source}`);
            const tsData = await timeseriesRes.json();
            setTimeseriesData(tsData);
            setChartSources([source]);
        } catch (err) {
            setError(err.message);
            console.error(err);
        } finally {
            setLoading(false);
        }
    }, [sources]);

    return {
        sources,
        allSourceOption: ALL_SOURCES_OPTION,
        dailyStats,
        setDailyStats,
        timeseriesData,
        chartSources,
        setTimeseriesData,
        loading,
        error,
        setError,
        fetchData
    };
};
