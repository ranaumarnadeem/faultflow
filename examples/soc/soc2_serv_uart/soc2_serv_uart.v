// soc2_serv_uart.v -- SERV (serial RISC-V) CPU + simpleuart peripheral SoC.
//
// Wiring modeled on SERV's own servant.v reference SoC (Copyright (C) 2019
// Olof Kindgren, ISC license): servile (CPU + bus) + RAM on the dedicated
// mem bus, with the ext bus routed to a UART instead of servant's stock
// gpio+timer pair (see servant_uart_mux.v). RAM/register-file storage
// reuses SERV's own servant_ram.v / serv_rf_ram.v unmodified.
`default_nettype none

module soc2_serv_uart #(
	parameter integer MEMSIZE        = 8192,
	parameter         RESET_STRATEGY = "MINI",
	parameter         WIDTH          = 1,
	parameter [0:0]   WITH_CSR       = 1'b1
) (
	input  wire clk,
	input  wire rst,

	output wire ser_tx,
	input  wire ser_rx
);

	localparam CSR_REGS = WITH_CSR * 4;
	localparam RF_WIDTH = WIDTH * 2;
	localparam RF_L2D   = $clog2((32 + CSR_REGS) * 32 / RF_WIDTH);

	wire        timer_irq = 1'b0;

	wire [31:0] wb_mem_adr;
	wire [31:0] wb_mem_dat;
	wire [ 3:0] wb_mem_sel;
	wire        wb_mem_we;
	wire        wb_mem_stb;
	wire [31:0] wb_mem_rdt;
	wire        wb_mem_ack;

	wire [31:0] wb_ext_adr;
	wire [31:0] wb_ext_dat;
	wire [ 3:0] wb_ext_sel;
	wire        wb_ext_we;
	wire        wb_ext_stb;
	wire [31:0] wb_ext_rdt;
	wire        wb_ext_ack;

	wire [ 3:0] uart_reg_div_we;
	wire [31:0] uart_reg_div_di, uart_reg_div_do;
	wire        uart_reg_dat_we, uart_reg_dat_re, uart_reg_dat_wait;
	wire [31:0] uart_reg_dat_di, uart_reg_dat_do;

	wire [RF_L2D-1:0]   rf_waddr;
	wire [RF_WIDTH-1:0] rf_wdata;
	wire                rf_wen;
	wire [RF_L2D-1:0]   rf_raddr;
	wire                rf_ren;
	wire [RF_WIDTH-1:0] rf_rdata;

	servile #(
		.width    (WIDTH),
		.sim      (1'b0),
		.debug    (1'b0),
		.with_c   (1'b0),
		.with_csr (WITH_CSR),
		.with_mdu (1'b0)
	) cpu (
		.i_clk       (clk),
		.i_rst       (rst),
		.i_timer_irq (timer_irq),

		.o_wb_mem_adr (wb_mem_adr),
		.o_wb_mem_dat (wb_mem_dat),
		.o_wb_mem_sel (wb_mem_sel),
		.o_wb_mem_we  (wb_mem_we),
		.o_wb_mem_stb (wb_mem_stb),
		.i_wb_mem_rdt (wb_mem_rdt),
		.i_wb_mem_ack (wb_mem_ack),

		.o_wb_ext_adr (wb_ext_adr),
		.o_wb_ext_dat (wb_ext_dat),
		.o_wb_ext_sel (wb_ext_sel),
		.o_wb_ext_we  (wb_ext_we),
		.o_wb_ext_stb (wb_ext_stb),
		.i_wb_ext_rdt (wb_ext_rdt),
		.i_wb_ext_ack (wb_ext_ack),

		.o_rf_waddr (rf_waddr),
		.o_rf_wdata (rf_wdata),
		.o_rf_wen   (rf_wen),
		.o_rf_raddr (rf_raddr),
		.o_rf_ren   (rf_ren),
		.i_rf_rdata (rf_rdata)
	);

	soc2_mem_subsystem #(
		.MEMSIZE        (MEMSIZE),
		.RESET_STRATEGY (RESET_STRATEGY),
		.RF_WIDTH       (RF_WIDTH),
		.RF_L2D         (RF_L2D),
		.CSR_REGS       (CSR_REGS)
	) mem_subsystem (
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

		.reg_div_we   (uart_reg_div_we),
		.reg_div_di   (uart_reg_div_di),
		.reg_div_do   (uart_reg_div_do),
		.reg_dat_we   (uart_reg_dat_we),
		.reg_dat_re   (uart_reg_dat_re),
		.reg_dat_di   (uart_reg_dat_di),
		.reg_dat_do   (uart_reg_dat_do),
		.reg_dat_wait (uart_reg_dat_wait)
	);

	simpleuart uart (
		.clk    (clk),
		.resetn (!rst),

		.ser_tx (ser_tx),
		.ser_rx (ser_rx),

		.reg_div_we (uart_reg_div_we),
		.reg_div_di (uart_reg_div_di),
		.reg_div_do (uart_reg_div_do),

		.reg_dat_we   (uart_reg_dat_we),
		.reg_dat_re   (uart_reg_dat_re),
		.reg_dat_di   (uart_reg_dat_di),
		.reg_dat_do   (uart_reg_dat_do),
		.reg_dat_wait (uart_reg_dat_wait)
	);

endmodule
