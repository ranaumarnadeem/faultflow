// servant_uart_mux.v -- Wishbone-lite bridge from SERV's servile ext bus to
// simpleuart's native register interface (div register + data register,
// matching the address convention used by picosoc.v). Modeled on SERV's
// own servant_mux.v (Copyright (C) 2019 Olof Kindgren, ISC license) but
// targets a UART register pair instead of servant's stock gpio+timer pair.
//
// Exposes the simpleuart register interface as its own ports rather than
// instantiating simpleuart internally -- the SoC-level glue wires this to
// the separately WBR-wrapped simpleuart block instance, so there is exactly
// one UART in the composed chip, not two.
`default_nettype none

module servant_uart_mux (
	input  wire        i_clk,
	input  wire        i_rst,
	input  wire [31:0] i_wb_cpu_adr,
	input  wire [31:0] i_wb_cpu_dat,
	input  wire [ 3:0] i_wb_cpu_sel,
	input  wire        i_wb_cpu_we,
	input  wire        i_wb_cpu_cyc,
	output wire [31:0] o_wb_cpu_rdt,
	output wire        o_wb_cpu_ack,

	output wire [ 3:0] reg_div_we,
	output wire [31:0] reg_div_di,
	input  wire [31:0] reg_div_do,

	output wire        reg_dat_we,
	output wire        reg_dat_re,
	output wire [31:0] reg_dat_di,
	input  wire [31:0] reg_dat_do,
	input  wire        reg_dat_wait
);

	wire sel_dat = i_wb_cpu_adr[2];

	wire uart_div_sel = i_wb_cpu_cyc && !sel_dat;
	wire uart_dat_sel = i_wb_cpu_cyc && sel_dat;

	assign o_wb_cpu_ack = uart_div_sel || (uart_dat_sel && !reg_dat_wait);
	assign o_wb_cpu_rdt = sel_dat ? reg_dat_do : reg_div_do;

	assign reg_div_we = (uart_div_sel && i_wb_cpu_we) ? i_wb_cpu_sel : 4'b0000;
	assign reg_div_di = i_wb_cpu_dat;

	assign reg_dat_we = uart_dat_sel && i_wb_cpu_we;
	assign reg_dat_re = uart_dat_sel && !i_wb_cpu_we;
	assign reg_dat_di = i_wb_cpu_dat;

endmodule
