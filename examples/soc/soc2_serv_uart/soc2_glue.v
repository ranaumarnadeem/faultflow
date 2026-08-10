// soc2_glue.v -- SoC2 (SERV + UART) interconnect stub for assemble_soc().
// AUTO-GENERATED (gen_soc2_glue.py) from the frozen servile/simpleuart
// scan+wrap JSON, matching soc1_glue.v's pattern. The memory subsystem
// (soc2_mem_subsystem) is deliberately NOT a spliced block here -- it's
// permanently blackboxed (see soc2_mem_subsystem_blackbox.v) via
// blackbox_instances=["mem_subsystem"] at ATPG time, not wrapped/scanned.
`default_nettype none

module soc2_glue (
	input  wire clk,
	input  wire rst,

	output wire ser_tx,
	input  wire ser_rx,

	input  wire soc_wbr_se,
	input  wire soc_wbr_si,
	output wire soc_wbr_so
);

	wire [31:0] wb_mem_adr, wb_mem_dat, wb_mem_rdt;
	wire [3:0]  wb_mem_sel;
	wire        wb_mem_we, wb_mem_stb, wb_mem_ack;

	wire [31:0] wb_ext_adr, wb_ext_dat, wb_ext_rdt;
	wire [3:0]  wb_ext_sel;
	wire        wb_ext_we, wb_ext_stb, wb_ext_ack;

	wire [8:0] rf_waddr, rf_raddr;
	wire [1:0] rf_wdata, rf_rdata;
	wire       rf_wen, rf_ren;

	wire wbr_mid;

	wire [3:0]  mux_reg_div_we;
	wire [31:0] mux_reg_div_di, mux_reg_div_do;
	wire        mux_reg_dat_we, mux_reg_dat_re, mux_reg_dat_wait;
	wire [31:0] mux_reg_dat_di, mux_reg_dat_do;

	wire n_sv_scan_en;
	wire n_sv_scan_in_0;
	wire n_sv_scan_in_1;
	wire n_sv_scan_in_10;
	wire n_sv_scan_in_11;
	wire n_sv_scan_in_12;
	wire n_sv_scan_in_13;
	wire n_sv_scan_in_14;
	wire n_sv_scan_in_15;
	wire n_sv_scan_in_16;
	wire n_sv_scan_in_17;
	wire n_sv_scan_in_18;
	wire n_sv_scan_in_19;
	wire n_sv_scan_in_2;
	wire n_sv_scan_in_3;
	wire n_sv_scan_in_4;
	wire n_sv_scan_in_5;
	wire n_sv_scan_in_6;
	wire n_sv_scan_in_7;
	wire n_sv_scan_in_8;
	wire n_sv_scan_in_9;
	wire n_sv_scan_out_0;
	wire n_sv_scan_out_1;
	wire n_sv_scan_out_10;
	wire n_sv_scan_out_11;
	wire n_sv_scan_out_12;
	wire n_sv_scan_out_13;
	wire n_sv_scan_out_14;
	wire n_sv_scan_out_15;
	wire n_sv_scan_out_16;
	wire n_sv_scan_out_17;
	wire n_sv_scan_out_18;
	wire n_sv_scan_out_19;
	wire n_sv_scan_out_2;
	wire n_sv_scan_out_3;
	wire n_sv_scan_out_4;
	wire n_sv_scan_out_5;
	wire n_sv_scan_out_6;
	wire n_sv_scan_out_7;
	wire n_sv_scan_out_8;
	wire n_sv_scan_out_9;

	servile cpu (
		.i_clk (clk),
		.i_rf_rdata (rf_rdata),
		.i_rst (rst),
		.i_timer_irq (1'b0),
		.i_wb_ext_ack (wb_ext_ack),
		.i_wb_ext_rdt (wb_ext_rdt),
		.i_wb_mem_ack (wb_mem_ack),
		.i_wb_mem_rdt (wb_mem_rdt),
		.o_rf_raddr (rf_raddr),
		.o_rf_ren (rf_ren),
		.o_rf_waddr (rf_waddr),
		.o_rf_wdata (rf_wdata),
		.o_rf_wen (rf_wen),
		.o_wb_ext_adr (wb_ext_adr),
		.o_wb_ext_dat (wb_ext_dat),
		.o_wb_ext_sel (wb_ext_sel),
		.o_wb_ext_stb (wb_ext_stb),
		.o_wb_ext_we (wb_ext_we),
		.o_wb_mem_adr (wb_mem_adr),
		.o_wb_mem_dat (wb_mem_dat),
		.o_wb_mem_sel (wb_mem_sel),
		.o_wb_mem_stb (wb_mem_stb),
		.o_wb_mem_we (wb_mem_we),
		.scan_en (n_sv_scan_en),
		.scan_in_0 (n_sv_scan_in_0),
		.scan_in_1 (n_sv_scan_in_1),
		.scan_in_10 (n_sv_scan_in_10),
		.scan_in_11 (n_sv_scan_in_11),
		.scan_in_12 (n_sv_scan_in_12),
		.scan_in_13 (n_sv_scan_in_13),
		.scan_in_14 (n_sv_scan_in_14),
		.scan_in_15 (n_sv_scan_in_15),
		.scan_in_16 (n_sv_scan_in_16),
		.scan_in_17 (n_sv_scan_in_17),
		.scan_in_18 (n_sv_scan_in_18),
		.scan_in_19 (n_sv_scan_in_19),
		.scan_in_2 (n_sv_scan_in_2),
		.scan_in_3 (n_sv_scan_in_3),
		.scan_in_4 (n_sv_scan_in_4),
		.scan_in_5 (n_sv_scan_in_5),
		.scan_in_6 (n_sv_scan_in_6),
		.scan_in_7 (n_sv_scan_in_7),
		.scan_in_8 (n_sv_scan_in_8),
		.scan_in_9 (n_sv_scan_in_9),
		.scan_out_0 (n_sv_scan_out_0),
		.scan_out_1 (n_sv_scan_out_1),
		.scan_out_10 (n_sv_scan_out_10),
		.scan_out_11 (n_sv_scan_out_11),
		.scan_out_12 (n_sv_scan_out_12),
		.scan_out_13 (n_sv_scan_out_13),
		.scan_out_14 (n_sv_scan_out_14),
		.scan_out_15 (n_sv_scan_out_15),
		.scan_out_16 (n_sv_scan_out_16),
		.scan_out_17 (n_sv_scan_out_17),
		.scan_out_18 (n_sv_scan_out_18),
		.scan_out_19 (n_sv_scan_out_19),
		.scan_out_2 (n_sv_scan_out_2),
		.scan_out_3 (n_sv_scan_out_3),
		.scan_out_4 (n_sv_scan_out_4),
		.scan_out_5 (n_sv_scan_out_5),
		.scan_out_6 (n_sv_scan_out_6),
		.scan_out_7 (n_sv_scan_out_7),
		.scan_out_8 (n_sv_scan_out_8),
		.scan_out_9 (n_sv_scan_out_9),
		.wbr_se (soc_wbr_se),
		.wbr_si (soc_wbr_si),
		.wbr_so (wbr_mid)
	);

	wire n_ua2_scan_en;
	wire n_ua2_scan_in_0;
	wire n_ua2_scan_in_1;
	wire n_ua2_scan_out_0;
	wire n_ua2_scan_out_1;

	simpleuart u_simpleuart (
		.clk (clk),
		.reg_dat_di (mux_reg_dat_di),
		.reg_dat_do (mux_reg_dat_do),
		.reg_dat_re (mux_reg_dat_re),
		.reg_dat_wait (mux_reg_dat_wait),
		.reg_dat_we (mux_reg_dat_we),
		.reg_div_di (mux_reg_div_di),
		.reg_div_do (mux_reg_div_do),
		.reg_div_we (mux_reg_div_we),
		.resetn (!rst),
		.scan_en (n_ua2_scan_en),
		.scan_in_0 (n_ua2_scan_in_0),
		.scan_in_1 (n_ua2_scan_in_1),
		.scan_out_0 (n_ua2_scan_out_0),
		.scan_out_1 (n_ua2_scan_out_1),
		.ser_rx (ser_rx),
		.ser_tx (ser_tx),
		.wbr_se (soc_wbr_se),
		.wbr_si (wbr_mid),
		.wbr_so (soc_wbr_so)
	);

	servant_uart_mux uart_mux (
		.i_clk        (clk),
		.i_rst        (rst),
		.i_wb_cpu_adr (wb_ext_adr),
		.i_wb_cpu_dat (wb_ext_dat),
		.i_wb_cpu_sel (wb_ext_sel),
		.i_wb_cpu_we  (wb_ext_we),
		.i_wb_cpu_cyc (wb_ext_stb),
		.o_wb_cpu_rdt (wb_ext_rdt),
		.o_wb_cpu_ack (wb_ext_ack),
		.reg_div_we   (mux_reg_div_we),
		.reg_div_di   (mux_reg_div_di),
		.reg_div_do   (mux_reg_div_do),
		.reg_dat_we   (mux_reg_dat_we),
		.reg_dat_re   (mux_reg_dat_re),
		.reg_dat_di   (mux_reg_dat_di),
		.reg_dat_do   (mux_reg_dat_do),
		.reg_dat_wait (mux_reg_dat_wait)
	);

	soc2_mem_subsystem mem_subsystem (
		.clk (clk),
		.rst (rst),
		.wb_mem_adr (wb_mem_adr),
		.wb_mem_dat (wb_mem_dat),
		.wb_mem_sel (wb_mem_sel),
		.wb_mem_we  (wb_mem_we),
		.wb_mem_stb (wb_mem_stb),
		.wb_mem_rdt (wb_mem_rdt),
		.wb_mem_ack (wb_mem_ack),
		.rf_waddr (rf_waddr),
		.rf_wdata (rf_wdata),
		.rf_wen   (rf_wen),
		.rf_raddr (rf_raddr),
		.rf_ren   (rf_ren),
		.rf_rdata (rf_rdata)
	);

endmodule

// Permanent blackbox stub for the memory subsystem -- see soc2_mem_subsystem's
// header comment on why (servant_ram alone is a 2048x32 array = 65,536 FFs;
// blackboxing via faultflow's existing Policy 1 mechanism (add_blackbox /
// blackbox_instances) is the methodologically honest treatment for an
// embedded memory this size, matching real DFT flows that hand memories this
// size to dedicated BIST rather than logic ATPG). No real body is defined
// anywhere else read during synthesis, so every instance of this module stays
// one opaque, unexpanded top-level cell for blackbox_instances to match.
(* blackbox *)
module soc2_mem_subsystem (
	input  wire        clk,
	input  wire        rst,
	input  wire [31:0] wb_mem_adr,
	input  wire [31:0] wb_mem_dat,
	input  wire [ 3:0] wb_mem_sel,
	input  wire        wb_mem_we,
	input  wire        wb_mem_stb,
	output wire [31:0] wb_mem_rdt,
	output wire        wb_mem_ack,
	input  wire [8:0]  rf_waddr,
	input  wire [1:0]  rf_wdata,
	input  wire        rf_wen,
	input  wire [8:0]  rf_raddr,
	input  wire        rf_ren,
	output wire [1:0]  rf_rdata
);
endmodule
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
