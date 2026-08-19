// soc1_picorv32a_uart.v -- picorv32a CPU + simpleuart peripheral SoC.
//
// Adapted from YosysHQ picosoc.v (Copyright (C) 2017 Claire Xenia Wolf,
// ISC license) with the SPI flash controller dropped and picorv32 swapped
// for picorv32a (this repo's proven, fully-featured variant). Memory-mapped
// register addresses for the UART match the picosoc convention unchanged.
`default_nettype none

module soc1_picorv32a_uart #(
	parameter integer MEM_WORDS     = 256,
	parameter [31:0]  STACKADDR     = (4 * MEM_WORDS),
	parameter [31:0]  PROGADDR_RESET = 32'h 0000_0000,
	parameter [31:0]  PROGADDR_IRQ   = 32'h 0000_0010
) (
	input  wire clk,
	input  wire resetn,

	output wire ser_tx,
	input  wire ser_rx
);

	wire        mem_valid;
	wire        mem_instr;
	wire        mem_ready;
	wire [31:0] mem_addr;
	wire [31:0] mem_wdata;
	wire [ 3:0] mem_wstrb;
	wire [31:0] mem_rdata;

	wire        mem_la_read;
	wire        mem_la_write;
	wire [31:0] mem_la_addr;
	wire [31:0] mem_la_wdata;
	wire [ 3:0] mem_la_wstrb;

	wire [31:0] pcpi_rs1;
	wire [31:0] pcpi_rs2;

	reg  ram_ready;
	wire [31:0] ram_rdata;

	wire        uart_div_sel = mem_valid && (mem_addr == 32'h 0200_0004);
	wire [31:0] uart_div_do;

	wire        uart_dat_sel = mem_valid && (mem_addr == 32'h 0200_0008);
	wire [31:0] uart_dat_do;
	wire        uart_dat_wait;

	assign mem_ready = ram_ready || uart_div_sel ||
	                    (uart_dat_sel && !uart_dat_wait);

	assign mem_rdata = ram_ready    ? ram_rdata  :
	                    uart_div_sel ? uart_div_do :
	                    uart_dat_sel ? uart_dat_do : 32'h 0000_0000;

	picorv32a #(
		.STACKADDR      (STACKADDR),
		.PROGADDR_RESET (PROGADDR_RESET),
		.PROGADDR_IRQ   (PROGADDR_IRQ),
		.ENABLE_PCPI    (1'b0),
		.ENABLE_MUL     (1'b0),
		.ENABLE_DIV     (1'b0),
		.ENABLE_IRQ     (1'b1),
		.ENABLE_IRQ_QREGS (1'b0)
	) cpu (
		.clk        (clk),
		.resetn     (resetn),
		.trap       (),

		.mem_valid  (mem_valid),
		.mem_instr  (mem_instr),
		.mem_ready  (mem_ready),
		.mem_addr   (mem_addr),
		.mem_wdata  (mem_wdata),
		.mem_wstrb  (mem_wstrb),
		.mem_rdata  (mem_rdata),

		.mem_la_read  (mem_la_read),
		.mem_la_write (mem_la_write),
		.mem_la_addr  (mem_la_addr),
		.mem_la_wdata (mem_la_wdata),
		.mem_la_wstrb (mem_la_wstrb),

		.pcpi_valid (),
		.pcpi_insn  (),
		.pcpi_rs1   (pcpi_rs1),
		.pcpi_rs2   (pcpi_rs2),
		.pcpi_wr    (1'b0),
		.pcpi_rd    (32'b0),
		.pcpi_wait  (1'b0),
		.pcpi_ready (1'b0),

		.irq (32'b0),
		.eoi ()
	);

	always @(posedge clk)
		ram_ready <= mem_valid && !mem_ready && mem_addr < 4 * MEM_WORDS;

	soc1_ram #(
		.WORDS (MEM_WORDS)
	) ram (
		.clk   (clk),
		.wen   ((mem_valid && !mem_ready && mem_addr < 4 * MEM_WORDS) ? mem_wstrb : 4'b0),
		.addr  (mem_addr[23:2]),
		.wdata (mem_wdata),
		.rdata (ram_rdata)
	);

	simpleuart uart (
		.clk    (clk),
		.resetn (resetn),

		.ser_tx (ser_tx),
		.ser_rx (ser_rx),

		.reg_div_we (uart_div_sel ? mem_wstrb : 4'b0000),
		.reg_div_di (mem_wdata),
		.reg_div_do (uart_div_do),

		.reg_dat_we   (uart_dat_sel ? mem_wstrb[0] : 1'b0),
		.reg_dat_re   (uart_dat_sel && !mem_wstrb),
		.reg_dat_di   (mem_wdata),
		.reg_dat_do   (uart_dat_do),
		.reg_dat_wait (uart_dat_wait)
	);

endmodule

module soc1_ram #(
	parameter integer WORDS = 256
) (
	input  wire        clk,
	input  wire [ 3:0] wen,
	input  wire [21:0] addr,
	input  wire [31:0] wdata,
	output reg  [31:0] rdata
);
	reg [31:0] mem [0:WORDS-1];

	always @(posedge clk) begin
		rdata <= mem[addr];
		if (wen[0]) mem[addr][ 7: 0] <= wdata[ 7: 0];
		if (wen[1]) mem[addr][15: 8] <= wdata[15: 8];
		if (wen[2]) mem[addr][23:16] <= wdata[23:16];
		if (wen[3]) mem[addr][31:24] <= wdata[31:24];
	end
endmodule
