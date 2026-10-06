"""Streamlit dashboard scaffold."""

import streamlit as st


st.set_page_config(page_title="Cloud Resource Optimizer", layout="wide")
st.title("Autonomous Cloud Resource Optimizer")
st.info("Dashboard scaffold. Connect this page to Prometheus and experiment data.")

col1, col2, col3, col4 = st.columns(4)
col1.metric("Replicas", "--")
col2.metric("CPU", "--")
col3.metric("p95 latency", "--")
col4.metric("Estimated savings", "--")

st.subheader("Implementation tasks")
st.markdown(
    """
    - Query live Prometheus metrics.
    - Plot CPU, memory, request rate, and latency.
    - Plot 1, 3, and 5 minute forecasts.
    - Display scaling events.
    - Compare reactive and predictive experiments.
    - Calculate estimated replica-minute cost and savings.
    """
)
