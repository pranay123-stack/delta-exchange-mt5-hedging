import React from "react";
import ReactDOM from "react-dom/client";
import { RouterProvider, createBrowserRouter } from "react-router-dom";
import { Layout } from "@/components/Layout";
import { Overview } from "@/pages/Overview";
import { HedgePairs } from "@/pages/HedgePairs";
import { Calculator } from "@/pages/Calculator";
import { RiskDashboard } from "@/pages/RiskDashboard";
import { Orders } from "@/pages/Orders";
import { Cycles } from "@/pages/Cycles";
import { Configuration } from "@/pages/Configuration";
import { FaultInjection } from "@/pages/FaultInjection";
import "./styles.css";

const router = createBrowserRouter([
  {
    path: "/",
    element: <Layout />,
    children: [
      { index: true, element: <Overview /> },
      { path: "pairs", element: <HedgePairs /> },
      { path: "calculator", element: <Calculator /> },
      { path: "risk", element: <RiskDashboard /> },
      { path: "orders", element: <Orders /> },
      { path: "cycles", element: <Cycles /> },
      { path: "configuration", element: <Configuration /> },
      { path: "faults", element: <FaultInjection /> },
    ],
  },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
);
