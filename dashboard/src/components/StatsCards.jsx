import React from 'react';

const StatCard = ({ title, value }) => (
    <div className="bg-gray-800 p-3 rounded-lg shadow-lg">
        <h3 className="text-gray-400 text-sm font-medium uppercase tracking-wider">{title}</h3>
        <p className="mt-2 text-2xl font-semibold text-white">{value}</p>
    </div>
);

const StatsCards = ({ dailyStats }) => {
    if (!dailyStats) return null;

    return (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-2">
            <StatCard title="Total Requests (Day)" value={dailyStats.total_requests.toLocaleString()} />
            <StatCard title="Successful (Day)" value={dailyStats.total_success.toLocaleString()} />
            <StatCard title="Success Rate (Day)" value={`${dailyStats.success_rate}%`} />
        </div>
    );
};

export default StatsCards;
