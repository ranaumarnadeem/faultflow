// soc3_tinyaes_uart.v -- tiny_aes (aes_128) crypto core + simpleuart
// peripheral SoC. No CPU: a small hand-written FSM shifts 32 bytes in over
// UART (16-byte plaintext + 16-byte key), lets the aes_128 pipeline settle,
// then shifts the 16-byte ciphertext back out. aes_128 itself comes from
// examples/fault_benchmarks/tiny_aes.v, unmodified. simpleuart comes from
// examples/soc/common/simpleuart.v (see soc1 for its license header).
`default_nettype none

module soc3_tinyaes_uart (
	input  wire clk,
	input  wire resetn,

	output wire ser_tx,
	input  wire ser_rx
);

	localparam integer NBYTES_IN    = 32; // 16 state bytes + 16 key bytes
	localparam integer NBYTES_OUT   = 16; // 16 ciphertext bytes
	localparam integer PIPE_LATENCY = 16; // cycles for the aes_128 pipeline to settle

	localparam [1:0] RECV  = 2'd0;
	localparam [1:0] FLUSH = 2'd1;
	localparam [1:0] SEND  = 2'd2;

	reg [1:0]   phase;
	reg [5:0]   byte_idx;
	reg [255:0] in_shift;
	reg [7:0]   flush_cnt;
	reg [127:0] out_shift;

	wire [127:0] aes_state_in = in_shift[255:128];
	wire [127:0] aes_key_in   = in_shift[127:0];
	wire [127:0] aes_out;

	aes_128 aes_core (
		.clk   (clk),
		.state (aes_state_in),
		.key   (aes_key_in),
		.out   (aes_out)
	);

	reg        uart_re;
	reg        uart_we;
	reg [31:0] uart_wdata;
	wire [31:0] uart_rdata;
	wire        uart_tx_wait;

	wire uart_byte_ready = !uart_rdata[31];
	wire [7:0] uart_rx_byte = uart_rdata[7:0];

	simpleuart #(
		.DEFAULT_DIV(867)
	) uart (
		.clk    (clk),
		.resetn (resetn),

		.ser_tx (ser_tx),
		.ser_rx (ser_rx),

		.reg_div_we (4'b0000),
		.reg_div_di (32'b0),
		.reg_div_do (),

		.reg_dat_we   (uart_we),
		.reg_dat_re   (uart_re),
		.reg_dat_di   (uart_wdata),
		.reg_dat_do   (uart_rdata),
		.reg_dat_wait (uart_tx_wait)
	);

	always @(posedge clk) begin
		uart_re <= 1'b0;
		uart_we <= 1'b0;

		if (!resetn) begin
			phase      <= RECV;
			byte_idx   <= 6'd0;
			in_shift   <= 256'd0;
			flush_cnt  <= 8'd0;
			out_shift  <= 128'd0;
			uart_wdata <= 32'd0;
		end else begin
			case (phase)
				RECV: begin
					uart_re <= 1'b1;
					if (uart_byte_ready) begin
						in_shift <= {in_shift[247:0], uart_rx_byte};
						if (byte_idx == NBYTES_IN - 1) begin
							byte_idx <= 6'd0;
							phase    <= FLUSH;
						end else begin
							byte_idx <= byte_idx + 1'b1;
						end
					end
				end

				FLUSH: begin
					if (flush_cnt == PIPE_LATENCY - 1) begin
						flush_cnt <= 8'd0;
						out_shift <= aes_out;
						phase     <= SEND;
					end else begin
						flush_cnt <= flush_cnt + 1'b1;
					end
				end

				SEND: begin
					if (!uart_tx_wait && !uart_we) begin
						uart_we    <= 1'b1;
						uart_wdata <= {24'b0, out_shift[127:120]};
						out_shift  <= {out_shift[119:0], 8'b0};
						if (byte_idx == NBYTES_OUT - 1) begin
							byte_idx <= 6'd0;
							phase    <= RECV;
						end else begin
							byte_idx <= byte_idx + 1'b1;
						end
					end
				end

				default: phase <= RECV;
			endcase
		end
	end

endmodule
