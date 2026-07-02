import { useState, useEffect, useCallback, useRef } from 'react';

const API_BASE_URL = '/api';
const ALL_SOURCES_OPTION = 'ALL';

const sameArray = (left, right) => (
    left.length === right.length && left.every((item, index) => item === right[index])
);

export const useDashboardData = () => {
    const [sources, setSources] = useState([]);
    const [dailyStats, setDailyStats] = useState(null);
    const [timeseriesData, setTimeseriesData] = useState([]);
    const [chartSources, setChartSources] = useState([]);
    const [loading, setLoading] = useState(false);
    const [isRefreshing, setIsRefreshing] = useState(false);
    const [error, setError] = useState(null);

    const sourcesRef = useRef([]);
    const requestIdRef = useRef(0);
    const activeControllerRef = useRef(null);

    useEffect(() => {
        sourcesRef.current = sources;
    }, [sources]);

    const fetchSources = useCallback(async (silent = false) => {
        try {
            if (!silent) setLoading(true);
            const response = await fetch(`${API_BASE_URL}/sources`);
            if (!response.ok) throw new Error('Failed to fetch source list');
            const data = await response.json();
            const normalized = Array.from(
                new Set((data || []).filter((source) => source && source !== ALL_SOURCES_OPTION))
            );
            const nextSources = [ALL_SOURCES_OPTION, ...normalized];
            setSources((previous) => (sameArray(previous, nextSources) ? previous : nextSources));
        } catch (err) {
            setError('Could not connect to the backend. Please ensure the proxy service is running.');
            console.error(err);
        } finally {
            if (!silent) setLoading(false);
        }
    }, []);

    useEffect(() => {
        fetchSources(false);
        const timer = setInterval(() => fetchSources(true), 30000);
        return () => clearInterval(timer);
    }, [fetchSources]);

    const fetchData = useCallback(async (source, date, interval, options = {}) => {
        if (!source || !date) return;

        const { silent = false } = options;
        if (activeControllerRef.current) {
            activeControllerRef.current.abort();
        }

        const controller = new AbortController();
        activeControllerRef.current = controller;
        const requestId = requestIdRef.current + 1;
        requestIdRef.current = requestId;

        if (silent) {
            setIsRefreshing(true);
        } else {
            setLoading(true);
        }
        setError(null);

        const isLatestRequest = () => requestIdRef.current === requestId && !controller.signal.aborted;

        try {
            if (source === ALL_SOURCES_OPTION) {
                const overviewRes = await fetch(
                    `${API_BASE_URL}/stats/overview?date=${encodeURIComponent(date)}&interval=${interval}`,
                    { signal: controller.signal }
                );
                if (!overviewRes.ok) throw new Error('Failed to fetch overview stats');

                const overview = await overviewRes.json();
                if (!isLatestRequest()) return;

                const fulfilled = overview?.sources || [];
                if (fulfilled.length === 0) {
                    setDailyStats({ total_requests: 0, total_success: 0, success_rate: 0 });
                    setTimeseriesData([]);
                    setChartSources([]);
                    return;
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
                        row[item.source] = point.success_rate || 0;
                        row[`${item.source}__req`] = point.total_requests || 0;
                        row._meta[item.source] = {
                            total_requests: point.total_requests || 0,
                            success_count: point.success_count || 0,
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

            const dailyRes = await fetch(
                `${API_BASE_URL}/stats/daily?source=${encodeURIComponent(source)}&date=${encodeURIComponent(date)}`,
                { signal: controller.signal }
            );
            if (!dailyRes.ok) throw new Error(`Failed to fetch daily stats for ${source}`);
            const dailyData = await dailyRes.json();
            if (!isLatestRequest()) return;
            setDailyStats(dailyData);

            const timeseriesRes = await fetch(
                `${API_BASE_URL}/stats/timeseries?source=${encodeURIComponent(source)}&date=${encodeURIComponent(date)}&interval=${interval}`,
                { signal: controller.signal }
            );
            if (!timeseriesRes.ok) throw new Error(`Failed to fetch timeseries data for ${source}`);
            const tsData = await timeseriesRes.json();
            if (!isLatestRequest()) return;
            setTimeseriesData(tsData);
            setChartSources([source]);
        } catch (err) {
            if (err.name === 'AbortError') return;
            if (isLatestRequest()) {
                setError(err.message);
            }
            console.error(err);
        } finally {
            if (isLatestRequest()) {
                activeControllerRef.current = null;
                setLoading(false);
                setIsRefreshing(false);
            }
        }
    }, []);

    return {
        sources,
        allSourceOption: ALL_SOURCES_OPTION,
        dailyStats,
        setDailyStats,
        timeseriesData,
        chartSources,
        setTimeseriesData,
        loading,
        isRefreshing,
        error,
        setError,
        fetchData
    };
};
