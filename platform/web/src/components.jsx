import React, { useEffect, useState, useCallback } from "react";

export function Badge({ status }) {
  return <span className={`badge b-${status}`}>{String(status).replace(/_/g, " ")}</span>;
}

// simple data hook with refresh + optional polling
export function useAsync(fn, deps = [], pollMs = 0) {
  const [state, setState] = useState({ loading: true, data: null, error: null });
  const load = useCallback(() => {
    fn().then((data) => setState({ loading: false, data, error: null }))
       .catch((error) => setState({ loading: false, data: null, error: error.message }));
  }, deps); // eslint-disable-line
  useEffect(() => {
    load();
    if (pollMs) { const t = setInterval(load, pollMs); return () => clearInterval(t); }
  }, [load, pollMs]);
  return { ...state, reload: load };
}

export function ErrorBox({ error }) {
  return error ? <div className="err" role="alert">{error}</div> : null;
}
