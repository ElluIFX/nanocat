import { render } from "preact";
import { App } from "./App";
import { ErrorBoundary } from "./components/ErrorBoundary";
import "./styles.css";

const root = document.getElementById("app");
if (!root) throw new Error("Application root is missing");

render(<ErrorBoundary><App /></ErrorBoundary>, root);
