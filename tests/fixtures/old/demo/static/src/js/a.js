/** @odoo-module **/
import { Component } from "@odoo/owl";

export class Foo extends Component {
    static template = "demo.Widget";
    static props = { a: String };
    setup() {}
    bar(a, b) {}
}
export function helper() {}
export const K = 1;
export default 5;
