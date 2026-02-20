import React, { useMemo } from 'react';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts';

const SOURCE_COLORS = [
    '#2DD4BF',
    '#60A5FA',
    '#F59E0B',
    '#F472B6',
    '#22C55E',
    '#A78BFA',
    '#FB7185',
    '#38BDF8',
    '#F97316',
    '#84CC16',
];

const CustomTooltip = ({ active, payload, label, mode, sourceColors, chartSources }) => {
    if (active && payload && payload.length) {
        const row = payload[0].payload;

        if (mode === 'all') {
            const entries = chartSources
                .map((source) => {
                    const meta = row?._meta?.[source] || {};
                    const totalRequests = meta.total_requests ?? row?.[`${source}__req`] ?? 0;
                    return {
                        source,
                        successRate: Number(row?.[source] || 0),
                        totalRequests,
                        successCount: meta.success_count || 0,
                    };
                })
                .sort((a, b) => a.source.localeCompare(b.source));

            return (
                <div className="bg-gray-800 p-3 rounded-lg border border-gray-700 shadow-lg text-sm">
                    <p className="label text-gray-300">{`Time : ${label}`}</p>
                    {entries.map((entry) => (
                        <div key={entry.source} className="mt-1 text-gray-200">
                            <p className="flex items-center gap-2">
                                <span
                                    className="inline-block w-2 h-2 rounded-full"
                                    style={{ backgroundColor: sourceColors[entry.source] || '#E5E7EB' }}
                                />
                                <span>{`${entry.source}: ${entry.successRate}%`}</span>
                            </p>
                            <p className="text-gray-400 pl-4">{`Success: ${entry.successCount.toLocaleString()} / Requests: ${entry.totalRequests.toLocaleString()}`}</p>
                        </div>
                    ))}
                </div>
            );
        }

        return (
            <div className="bg-gray-800 p-3 rounded-lg border border-gray-700 shadow-lg text-sm">
                <p className="label text-gray-300">{`Time : ${label}`}</p>
                <p className="intro text-cyan-400">{`Interval Success Rate : ${row.success_rate}%`}</p>
                <p className="desc text-gray-400">{`Successful Requests: ${row.success_count.toLocaleString()}`}</p>
                <p className="desc text-gray-400">{`Interval Requests: ${row.total_requests.toLocaleString()}`}</p>
            </div>
        );
    }
    return null;
};

const Charts = ({
    timeseriesData,
    selectedSource,
    allSourceOption,
    chartSources,
    loading,
    interval,
    setInterval,
    validIntervals,
    dailyStats,
}) => {
    const isAllMode = selectedSource === allSourceOption;

    const sourceColors = useMemo(() => {
        const colors = {};
        chartSources.forEach((source, index) => {
            colors[source] = SOURCE_COLORS[index % SOURCE_COLORS.length];
        });
        return colors;
    }, [chartSources]);

    // Generate ticks for every 2 hours: 00:00, 02:00, ... 22:00
    const ticks = Array.from({ length: 12 }, (_, i) => {
        const hour = i * 2;
        return `${String(hour).padStart(2, '0')}:00`;
    });

    const hasChartData = timeseriesData.length > 0 && (!isAllMode || chartSources.length > 0);

    return (
        <div className="bg-gray-800 p-2 rounded-lg shadow-lg">
            <div className="flex justify-between items-center mb-4">
                <h2 className="text-xl font-semibold text-gray-200">Success Rate by Time Interval</h2>
                <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm text-gray-400">Interval:</span>
                    {validIntervals.map(val => (
                        <button
                            key={val}
                            onClick={() => setInterval(val)}
                            className={`px-3 py-1 text-sm rounded-md transition-colors ${interval === val ? 'bg-cyan-600 text-white' : 'bg-gray-700 hover:bg-gray-600'}`}
                        >
                            {val}m
                        </button>
                    ))}
                </div>
            </div>
            <div style={{ width: '100%', height: 600 }}>
                <ResponsiveContainer>
                    {loading ? (
                        <div className="flex items-center justify-center h-full text-gray-500">Loading chart data...</div>
                    ) : hasChartData ? (
                        <LineChart data={timeseriesData} margin={{ top: 5, right: 20, left: 0, bottom: 5 }}>
                            <CartesianGrid strokeDasharray="3 3" stroke="#4A5568" />
                            <XAxis dataKey="time" stroke="#A0AEC0" ticks={ticks} />
                            <YAxis yAxisId="left" stroke="#2DD4BF" unit="%" domain={isAllMode ? [-1, 100] : [0, 100]} />
                            <YAxis yAxisId="right" orientation="right" stroke="#94A3B8" />
                            <Tooltip content={<CustomTooltip mode={isAllMode ? 'all' : 'single'} sourceColors={sourceColors} chartSources={chartSources} />} />
                            <Legend wrapperStyle={{ color: '#E2E8F0' }} />
                            {isAllMode ? (
                                <>
                                    {chartSources.map((source) => (
                                        <Line
                                            key={`${source}-rate`}
                                            yAxisId="left"
                                            type="monotone"
                                            dataKey={source}
                                            name={`${source} Rate`}
                                            stroke={sourceColors[source]}
                                            strokeWidth={2}
                                            dot={false}
                                            connectNulls
                                        />
                                    ))}
                                    {chartSources.map((source) => (
                                        <Line
                                            key={`${source}-req`}
                                            yAxisId="right"
                                            type="monotone"
                                            dataKey={`${source}__req`}
                                            name={`${source} Req`}
                                            stroke={sourceColors[source]}
                                            strokeDasharray="6 4"
                                            strokeOpacity={0.65}
                                            strokeWidth={1.5}
                                            dot={false}
                                            connectNulls
                                        />
                                    ))}
                                </>
                            ) : (
                                <>
                                    <Line yAxisId="left" type="monotone" dataKey="success_rate" name="Success Rate" stroke="#2DD4BF" strokeWidth={2} dot={false} />
                                    <Line yAxisId="right" type="monotone" dataKey="total_requests" name="Request Count" stroke="#8884d8" strokeWidth={2} dot={false} />
                                </>
                            )}
                        </LineChart>
                    ) : (
                        <div className="flex items-center justify-center h-full text-gray-500">
                            {dailyStats ? 'No data available for the selected date.' : 'Please click "Search" to load data.'}
                        </div>
                    )}
                </ResponsiveContainer>
            </div>
        </div>
    );
};

export default Charts;
