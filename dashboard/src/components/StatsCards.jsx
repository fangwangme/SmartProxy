import React from 'react';

const StatCard = ({ title, value }) => (
    <div className="bg-gray-800 p-3 rounded-lg shadow-lg">
        <h3 className="text-gray-400 text-sm font-medium uppercase tracking-wider">{title}</h3>
        <p className="mt-2 text-2xl font-semibold text-white">{value}</p>
    </div>
);

const StatsCards = ({ dailyStats }) => {
    if (!dailyStats) return null;

    const totalRequests = Number(dailyStats?.total_requests ?? 0);
    const totalSuccess = Number(dailyStats?.total_success ?? 0);
    const successRate = Number(dailyStats?.success_rate ?? 0);

    return (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-2">
            <StatCard title="Total Requests (Day)" value={totalRequests.toLocaleString()} />
            <StatCard title="Successful (Day)" value={totalSuccess.toLocaleString()} />
            <StatCard title="Success Rate (Day)" value={`${successRate}%`} />
        </div>
    );
};

export default StatsCards;
