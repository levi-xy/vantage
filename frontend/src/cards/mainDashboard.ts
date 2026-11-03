import { createContext } from "react";

/**
 * True while a card is rendered on the main dashboard (the first one). The backend pushes
 * `live_rate` for the main dashboard's throughput card only, so that card uses the pushed
 * rate only there; on any other dashboard it shows Prometheus' rate.
 */
export const MainDashboardContext = createContext(false);
