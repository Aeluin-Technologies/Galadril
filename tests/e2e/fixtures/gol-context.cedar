permit(principal is Gol::Principal, action, resource)
when { context.hour_utc >= 0 && context.hour_utc <= 23 };
