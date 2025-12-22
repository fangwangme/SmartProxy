import React from 'react';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts';

const CustomTooltip = ({ active, payload, label }) => {
    if (active && payload && payload.length) {
        const data = payload[0].payload;
        return (
            <div className="bg-gray-800 p-3 rounded-lg border border-gray-700 shadow-lg text-sm">
                <p className="label text-gray-300">{`Time : ${label}`}</p>
                <p className="intro text-cyan-400">{`Interval Success Rate : ${data.success_rate}%`}</p>
                <p className="desc text-gray-400">{`Successful Requests: ${data.success_count.toLocaleString()}`}</p>
                <p className="desc text-gray-400">{`Interval Requests: ${data.total_requests.toLocaleString()}`}</p>
            </div>
        );
    }
    return null;
};

const Charts = ({ timeseriesData, loading, interval, setInterval, validIntervals, dailyStats }) => {
    // Generate ticks for every 2 hours: 00:00, 02:00, ... 22:00
    const ticks = Array.from({ length: 12 }, (_, i) => {
        const hour = i * 2;
        return `${String(hour).padStart(2, '0')}:00`;
    });

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
                    ) : timeseriesData.length > 0 ? (
                        <LineChart data={timeseriesData} margin={{ top: 5, right: 20, left: 0, bottom: 5 }}>
                            <CartesianGrid strokeDasharray="3 3" stroke="#4A5568" />
                            <XAxis dataKey="time" stroke="#A0AEC0" ticks={ticks} />
                            <YAxis yAxisId="left" stroke="#2DD4BF" unit="%" domain={[0, 100]} />
                            <YAxis yAxisId="right" orientation="right" stroke="#8884d8" />
                            <Tooltip content={<CustomTooltip />} />
                            <Legend wrapperStyle={{ color: '#E2E8F0' }} />
                            <Line yAxisId="left" type="monotone" dataKey="success_rate" name="Success Rate" stroke="#2DD4BF" strokeWidth={2} dot={false} />
                            <Line yAxisId="right" type="monotone" dataKey="total_requests" name="Request Count" stroke="#8884d8" strokeWidth={2} dot={false} />
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
