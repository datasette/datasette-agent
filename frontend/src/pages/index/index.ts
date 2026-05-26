import "../../app.css";
import { mount } from "svelte";
import IndexPage from "./IndexPage.svelte";

const app = mount(IndexPage, {
  target: document.getElementById("app-root")!,
});

export default app;
