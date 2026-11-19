/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
patch(Foo.prototype, { setup() {}, extra: 2, more() {} });
