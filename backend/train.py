# backend/train.py
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
import joblib


data = {
    'distance_remaining_km': [2, 5, 10, 3, 7],
    'avg_speed': [30, 25, 40, 20, 35],
    'traffic_index': [0.3, 0.7, 0.5, 0.8, 0.4],
    'hour_of_day': [9, 18, 14, 20, 8],
    'eta_minutes': [5, 20, 15, 25, 12]
}

df = pd.DataFrame(data)

# Features & target
X = df[['distance_remaining_km', 'avg_speed', 'traffic_index', 'hour_of_day']]
y = df['eta_minutes']

# Train model
model = RandomForestRegressor(n_estimators=100, random_state=42)
model.fit(X, y)

# Save model
joblib.dump(model, "model.pkl")
print("✅ Model trained and saved as model.pkl")
